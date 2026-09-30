"""Ask-mode read-only command classifier.

`is_read_only_command()` is the gate `ModeAccessController.ensure_cloud_command_allowed()`
uses to decide what a cloud CLI may run in Ask mode, which is the mode every
background RCA runs in. It answers one question — *is this a write?* — and defers
everything about *danger* to the shared guardrail layers (`signature_match`,
`command_policy`, the LLM judge) reached via `utils.auth.command_gate`.

It cannot delegate to those layers: they are HITL (there is no user to prompt
during a background RCA), org-configurable, and switchable off entirely, while
Ask mode still has to hold. Conversely `kubectl delete pod x` is a routine
operation no guardrail should block, yet it must fail here.

Design rules, in order of importance:

1. **Default deny.** An unknown operation, an unparseable command, or anything
   this module cannot confidently describe is *not* read-only.
2. **Gate on shape, never on a list of vendor names.** Enumerating CLIs breaks
   real providers the moment one is missing (OVH's RCA skill emits a bare
   `cloud project list`), and enumerating interpreters is a denylist that is
   always one entry behind. Allowlist the narrow thing (text filters) and let
   everything else fall through to the default deny.
3. **Classify the operation, not the command line.** Option names and values can
   never supply a verb.
"""

from __future__ import annotations

import re
import shlex
from typing import NamedTuple, Optional

# Verbs that read. A hyphenated operation matches on its leading word, so
# `describe-health-check` -> 'describe'.
READ_ONLY_VERBS = frozenset({
    'list', 'ls', 'describe', 'get', 'show', 'config', 'version', 'info',
    'status', 'read', 'view', 'help', 'logs', 'log', 'top', 'explain', 'diff',
    'search', 'query', 'export', 'devices', 'routes', 'settings', 'check',
})

# Verbs that mutate. Only a verb's leading word is matched, so the common
# hyphenated cloud mutations (`modify-*`, `terminate-*`) are listed explicitly.
WRITE_VERBS = frozenset({
    'delete', 'destroy', 'remove', 'rm', 'create', 'apply', 'update', 'set',
    'patch', 'replace', 'edit', 'scale', 'drain', 'cordon', 'uncordon', 'exec',
    'attach', 'port-forward', 'cp', 'run', 'restart', 'start', 'stop', 'add',
    'put', 'write', 'invoke', 'rollout', 'taint', 'label', 'annotate', 'login',
    'modify', 'terminate', 'reboot', 'enable', 'disable', 'detach', 'associate',
    'disassociate', 'register', 'deregister', 'authorize', 'revoke', 'import',
    'reset', 'restore', 'purge', 'promote', 'publish', 'cancel', 'deploy',
    'install', 'uninstall', 'upgrade', 'rollback', 'grant', 'resize', 'resume',
    'suspend', 'migrate', 'move', 'rename', 'reimage', 'redeploy', 'reinstall',
    # State toggles and un-prefixed mutations. `logs tag-log-group` and
    # `config unset` read like reads but write.
    'tag', 'untag', 'unset', 'clear', 'logout', 'activate', 'deactivate',
    'abort', 'flush', 'prune', 'truncate', 'drop', 'sync', 'push', 'upload',
    'send', 'trigger', 'bind', 'unbind', 'mount', 'unmount', 'use', 'switch',
    'swap', 'rotate', 'renew', 'force',
})

# Boolean switches take no value, so the token after them is still part of the
# operation (`kubectl --insecure-skip-tls-verify get pods` is a read). Every
# other option is assumed to consume its value, which keeps an option value from
# being mistaken for the verb. Ambiguous single-letter aliases are left out on
# purpose -- `-w` is `--watch` in kubectl but `--workspace` in az.
VALUELESS_FLAGS = frozenset({
    # kubectl
    '--all-namespaces', '--all', '--insecure-skip-tls-verify', '--no-headers',
    '--show-labels', '--show-kind', '--ignore-not-found', '--recursive',
    '--follow', '--previous', '--timestamps', '--watch', '--watch-only',
    '--prefix', '--force', '--now', '--wait', '--no-wait',
    # aws
    '--no-cli-pager', '--no-paginate', '--no-verify-ssl', '--no-sign-request',
    '--with-decryption', '--no-cli-auto-prompt', '--cli-auto-prompt',
    # az / gcloud
    '--only-show-errors', '--include-preview', '--show-details', '--yes',
    '--quiet', '--verbose', '--debug', '--help', '--version',
})

# A read-only command may pipe into a text filter, and nothing else. This is an
# allowlist on purpose: an unrecognised downstream program is refused rather than
# guessed at, which is what stops a legitimate read from being used as a payload
# source. `kubectl get cm evil -o jsonpath='{.data.sh}' | bash` has a perfectly
# read-only first segment but executes whatever the ConfigMap holds, and `bash`
# is not a write verb. Keeping this fail-closed means interpreters and exfil
# tools (sh, python, xargs, ssh, nc, tee, ...) need no enumeration -- they simply
# aren't filters.
DOWNSTREAM_FILTERS = frozenset({
    'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack', 'jq', 'yq', 'head', 'tail',
    'sort', 'uniq', 'wc', 'cut', 'tr', 'awk', 'gawk', 'sed', 'tac',
    'column', 'nl', 'rev', 'paste', 'fold', 'expand',
    'echo', 'printf', 'strings', 'od', 'hexdump', 'less', 'more', 'seq',
})

# Commands whose arguments are themselves a command: the verb this module would
# classify belongs to a nested invocation, or runs under different privileges, so
# the classification would not describe what actually executes. `bash -c "..."`
# needs no entry -- its payload is an option value, already excluded from the
# operation, leaving no verb at all.
COMMAND_WRAPPERS = frozenset({
    'sudo', 'doas', 'su', 'env', 'xargs', 'nohup', 'setsid', 'timeout',
    'watch', 'time', 'nice', 'ionice', 'stdbuf', 'script',
})

# CLIs that take their operands as positional arguments after the verb, so the
# tokens to the right of the verb are resource names rather than operations
# (`kubectl logs update-cache-cronjob-xxx`). Everything else is assumed to spell
# its operation as the last positional, and gets every positional scanned for
# write verbs. Membership here only *relaxes* the scan, so an absent CLI
# over-blocks rather than under-blocks -- the safe direction for this gate.
OPERAND_POSITIONAL_CLIS = frozenset({'kubectl'})

# Some reads mutate nothing but hand back usable credentials, keys, secrets,
# connection strings or bearer tokens. Ask mode must refuse them outright,
# including hyphenated forms whose leading word ('get') looks read-only. One
# credential word in the operation is enough: every provider spells these the
# same way (`az signalr key list`, `az functionapp keys list`, `aws sts
# get-session-token`), so this covers the long tail of services that an explicit
# per-service deny list keeps missing.
CREDENTIAL_WORDS = frozenset({
    'key', 'keys', 'credential', 'credentials', 'secret', 'secrets',
    'password', 'passwords', 'token', 'tokens', 'sas', 'kerberos',
})

# Fetching a kubeconfig only writes a local file and is the required first step
# of every managed-Kubernetes investigation, so it stays allowed. Matched against
# the leading positionals in order, never an unordered word set: otherwise
# `kubectl get secret x aks get credentials` would smuggle the exemption's words
# into scope and unlock a real secret read.
CREDENTIAL_WORD_EXEMPT = (
    ('az', 'aks', 'get-credentials'),
    ('aks', 'get-credentials'),  # provider prefix already stripped by the caller
    ('gcloud', 'container', 'clusters', 'get-credentials'),
    ('container', 'clusters', 'get-credentials'),
)

# Options that turn an ordinary read into a secret dump.
CREDENTIAL_FLAGS = frozenset({
    '--with-decryption',   # aws ssm get-parameter
    '--expand-keys',       # az iot hub policy show
    '--include-keys', '--show-keys', '--show-secrets', '--reveal',
})

# Credential reads that contain none of the CREDENTIAL_WORDS.
CREDENTIAL_READS = (
    # any provider: `show-connection-string` embeds the account key
    ('connection', 'string'),
    # gcloud
    ('iam', 'service', 'accounts', 'sign', 'jwt'),
    ('iam', 'service', 'accounts', 'sign', 'blob'),
    # aws
    ('sts', 'assume', 'role'),
    ('lightsail', 'get', 'instance', 'access', 'details'),
    ('cognito', 'idp', 'initiate', 'auth'),
)

# Shell control characters that start a new command.
_CONTROL_CHARS = '();<>|&`'

# Operation words are split on every separator a CLI uses to glue a resource type
# to something else, not just '-'. kubectl accepts `secret,pods`, `secret/my-tls`
# and `secrets.v1`, so splitting on '-' alone let a credential word hide inside a
# compound token.
_WORD_SEP = re.compile(r'[-.,/:=@]+')
# The same separators minus '-', for matching a token that *is* a credential
# word: hyphens are part of ordinary resource names (`api-key-service`).
_RESOURCE_SEP = re.compile(r'[.,/:=@]+')


def _split_words(token: str) -> list[str]:
    return [w for w in _WORD_SEP.split(token) if w]


class SegmentFacts(NamedTuple):
    """What one shell segment contributes to the classification."""

    executable: Optional[str]
    verb: Optional[str]
    write_verb: Optional[str]  # any write verb in the operation, not just the first
    operation: tuple  # positional args in order, for prefix-anchored matching
    words: frozenset  # every operation word, for the CREDENTIAL_READS combos
    credential_words: frozenset  # words the credential rules are matched against


def split_segments(command: str) -> Optional[list[list[str]]]:
    """Tokenize *command* and split it into shell segments.

    Returns ``None`` when the command must be refused outright: unbalanced
    quotes, or a redirection (which writes a file or an fd no matter how
    read-only the command producing the bytes is).
    """
    segments, _reason = _split_segments_or_reason(command)
    return segments


def _split_segments_or_reason(
    command: str,
) -> tuple[Optional[list[list[str]]], str]:
    """`split_segments`, plus why it refused, for the Ask-mode error message."""
    # Newlines separate commands but shlex treats them as plain whitespace, so
    # they are turned into an explicit separator first.
    normalized = command.replace('\r', '\n').replace('\n', ' ; ')

    try:
        # punctuation_chars keeps `&&`, `||`, `|`, `;`, `(` and backticks as their
        # own tokens while leaving quoted operators (a KQL query's pipes) inside
        # the string they belong to.
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=_CONTROL_CHARS)
        lexer.whitespace_split = True
        raw_tokens = list(lexer)
    except ValueError:
        return None, 'it could not be parsed (check for an unbalanced quote)'

    segments: list[list[str]] = [[]]
    for token in raw_tokens:
        if token and all(c in _CONTROL_CHARS for c in token):
            # A redirection writes a file or an fd, which is a mutation.
            if '>' in token or '<' in token:
                return None, (
                    "it redirects with '%s', which writes a file regardless of "
                    'what produced the output' % token
                )
            segments.append([])
            continue
        segments[-1].append(token)

    trimmed = [seg for seg in segments if seg]
    if not trimmed:
        return None, 'it is empty'
    return trimmed, ''


def _operation_of(tokens: list[str]) -> list[str]:
    """Return the positional arguments of one segment, lowercased.

    Option names and their values are dropped so neither can supply the verb.
    """
    operation: list[str] = []
    skip_next = False
    for token in tokens:
        # Everything after `--` is passed to the target process, not the CLI.
        if token == '--':
            break
        # Previous token was a value-taking flag, so this is its value.
        if skip_next:
            skip_next = False
            continue
        # `--flag value` consumes the next token; `--flag=value` and known
        # boolean switches do not.
        if token.startswith('-'):
            skip_next = '=' not in token and token.lower() not in VALUELESS_FLAGS
            continue
        operation.append(token.lower())
    return operation


def _executable_of(operation: list[str]) -> Optional[str]:
    """The segment's program, with any path and `VAR=value` prefix stripped."""
    for token in operation:
        # A leading `VAR=value` assignment is not the command.
        if '=' in token and not token.startswith('/'):
            continue
        return token.rsplit('/', 1)[-1]
    return None


def _verb_of(token: str) -> Optional[str]:
    """The verb *token* denotes, matching on its leading word for hyphenated ops."""
    for candidate in (token, token.split('-', 1)[0]):
        if candidate in WRITE_VERBS or candidate in READ_ONLY_VERBS:
            return candidate
    return None


def _find_verb(operation: list[str]) -> tuple[Optional[str], int]:
    """The first positional that is a known verb, plus its index.

    A service or group name can itself be a read verb (`aws logs`, `az search`),
    so this verb says what the command *looks* like but cannot clear it of being
    a write -- that is `_find_write_verb`'s job.
    """
    for i, token in enumerate(operation):
        verb = _verb_of(token)
        if verb:
            return verb, i
    return None, len(operation)


def _find_write_verb(operation: list[str], executable: Optional[str],
                     verb_index: int) -> Optional[str]:
    """The first write verb anywhere in the operation, or None.

    Stopping at the first verb missed every mutation whose *service* is spelled
    like a read verb (`aws logs delete-log-group`, `gcloud config set`), so all
    positionals are scanned. kubectl is exempt because its operands are
    positional names (`kubectl logs update-cache-xxx`); an absent CLI gets the
    full scan and over-blocks, which is the safe direction.
    """
    scanned = (operation[:verb_index + 1]
               if executable in OPERAND_POSITIONAL_CLIS else operation)
    for token in scanned:
        if _verb_of(token) in WRITE_VERBS:
            return token
    return None


def _credential_words_of(operation: list[str], verb_index: int) -> set[str]:
    """Words the credential rules are matched against, for one segment.

    Matched two ways, so `az storage account keys list` and
    `aws sts get-session-token` are caught while a pod named `api-key-service`
    is not.
    """
    # Any positional that *is* a credential word once kubectl's resource syntax
    # is peeled off, so `secret,pods` / `secret/my-tls` / `secrets.v1` all match.
    # Hyphens are deliberately not split here: that would make every `*-key-*`
    # resource name a credential read.
    found = {
        w for t in operation for w in _RESOURCE_SEP.split(t)
        if w in CREDENTIAL_WORDS
    }

    # Plus the components of hyphenated tokens inside the operation path. That
    # path ends at the last positional that is *exactly* a verb, since a service
    # can precede the operation (`az search admin-key show`); trailing resource
    # names sit past it and are left out.
    exact_verbs = [
        i for i, t in enumerate(operation)
        if t in WRITE_VERBS or t in READ_ONLY_VERBS
    ]
    path_end = max([verb_index, *exact_verbs])
    for token in operation[:path_end + 1]:
        found.update(_split_words(token))
    return found


def analyze_segment(tokens: list[str]) -> SegmentFacts:
    """Reduce one shell segment to the facts the deny rules need."""
    operation = _operation_of(tokens)
    verb, verb_index = _find_verb(operation)
    executable = _executable_of(operation)
    return SegmentFacts(
        executable=executable,
        verb=verb,
        write_verb=_find_write_verb(operation, executable, verb_index),
        operation=tuple(operation),
        words=frozenset(w for token in operation for w in _split_words(token)),
        credential_words=frozenset(_credential_words_of(operation, verb_index)),
    )


def _is_kubeconfig_fetch(operation: tuple) -> bool:
    """Whether the operation is exactly a managed-Kubernetes kubeconfig fetch.

    Anchored to the leading positionals in order, so credential words elsewhere
    in the command cannot unlock the exemption.
    """
    return any(
        operation[:len(combo)] == combo for combo in CREDENTIAL_WORD_EXEMPT
    )


def _is_credential_read(facts: list, flag_names: set) -> bool:
    """Whether the command hands back credentials, keys, secrets or tokens."""
    # Options that dump secrets gate the whole command.
    if flag_names & CREDENTIAL_FLAGS:
        return True

    # A credential word in any segment blocks, unless *that* segment is exactly a
    # kubeconfig fetch. Checked per segment so one exempt segment can't clear a
    # credential read sitting in another.
    for f in facts:
        if f.credential_words & CREDENTIAL_WORDS and not _is_kubeconfig_fetch(f.operation):
            return True

    all_words = set().union(*(f.words for f in facts))
    return any(
        all(part in all_words for part in combo) for combo in CREDENTIAL_READS
    )


# `--dry-run` only makes an unknown operation safe when it actually skips
# execution. kubectl's `--dry-run=none` performs the real mutation, and
# `=false` is the same trap, so the value is checked rather than dropped.
_DRY_RUN_SKIPS_EXECUTION = frozenset({'client', 'server', 'true'})


def _is_real_dry_run(segments: list) -> bool:
    """Whether every segment carries a `--dry-run` that genuinely skips writes."""
    found = False
    for tokens in segments:
        for token in tokens:
            name, _, value = token.partition('=')
            if name.lower() != '--dry-run':
                continue
            # `--dry-run=none` / `=false` execute for real.
            if value and value.lower() not in _DRY_RUN_SKIPS_EXECUTION:
                return False
            found = True
    return found


def describe_rejection(command: str) -> str:
    """Why *command* is not read-only, as a clause, or '' when it is read-only.

    The agent sees this in the blocked tool's error, so it says which token
    caused the refusal: 'modifies infrastructure' alone is not enough to repair
    a command with.
    """
    segments, parse_reason = _split_segments_or_reason(command)
    if not segments:
        return parse_reason

    facts = [analyze_segment(seg) for seg in segments]

    # Flag names gate the command (`--with-decryption`, `--dry-run`) but must
    # never provide a read-only verb, so they are collected separately.
    flag_names = {
        t.split('=', 1)[0].lower() for seg in segments for t in seg
        if t.startswith('-') and t != '--'
    }

    secret_flags = flag_names & CREDENTIAL_FLAGS
    if secret_flags:
        return ("'%s' returns a decrypted secret, which Ask mode never allows"
                % sorted(secret_flags)[0])

    for f in facts:
        creds = f.credential_words & CREDENTIAL_WORDS
        if creds and not _is_kubeconfig_fetch(f.operation):
            return ("it returns a credential ('%s'), which Ask mode never allows"
                    % sorted(creds)[0])

    if _is_credential_read(facts, flag_names):
        return 'it returns usable credentials, which Ask mode never allows'

    # The offending token *and* the leading word it matched. Shape cannot tell a
    # real `delete-log-group` from a resource named `delete-me-vm`, so one wording
    # covers both: naming the leading word makes a misparse self-evident.
    for f in facts:
        if f.write_verb:
            verb = _verb_of(f.write_verb)
            if verb == f.write_verb:
                return "'%s' is a write operation" % f.write_verb
            return ("'%s' is a write operation (it leads with '%s')"
                    % (f.write_verb, verb))

    if facts[0].executable in COMMAND_WRAPPERS:
        return ("'%s' runs another command, so what would actually execute "
                "cannot be checked" % facts[0].executable)

    for f in facts[1:]:
        if f.executable not in DOWNSTREAM_FILTERS:
            return ("its output is piped into '%s', which is not a text filter"
                    % (f.executable or '?'))

    if facts[0].verb in READ_ONLY_VERBS or _is_real_dry_run(segments):
        return ''

    # Unknown or absent operation.
    if facts[0].verb is None:
        return 'no recognised read verb was found in it'
    return "'%s' is not a recognised read-only operation" % facts[0].verb


def is_read_only_command(command: str) -> bool:
    """Whether *command* only reads, and is therefore allowed in Ask mode.

    Matches on the command's verb tokens, not a substring scan: a bare
    `verb in command` test classifies `az group delete --name my-logs-rg` as
    read-only because "logs" appears in the resource name.

    Classification looks only at the *operation* (the positional arguments), so
    neither an option name nor an option value can supply the verb
    (`aws ec2 terminate-instances --query Reservations` stays blocked). The
    command is split into shell segments and every deny rule applies across all
    of them, so a write behind `&&`, a pipe, a newline or a `$(...)` cannot ride
    in on a read-only first segment. Only the leading segment may carry the read
    verb; the rest must be text filters.

    Defaults to False: anything unrecognised is not read-only.
    """
    return not describe_rejection(command)
