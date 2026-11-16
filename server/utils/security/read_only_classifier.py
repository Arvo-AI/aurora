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

# Subcommand groups of the CLIs above whose operation continues one token
# further, so the relaxed scan must not stop at the group name. `kubectl config`
# reads, but `config set-context` / `delete-context` / `unset` rewrite the
# kubeconfig, and stopping at 'config' let every one of them through. The token
# after such a group is a real operation, never an operand name.
SUBCOMMAND_GROUPS = frozenset({'config'})

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

# Reads that return a credential only through an option, or through an operand
# that sits past the verb. Anchored to the leading positionals, like the
# kubeconfig exemption, so the words cannot be smuggled in from elsewhere.
# `kubectl config view` prints the kubeconfig with its keys and tokens redacted
# unless `--raw` or `--flatten` is passed. `gcloud config config-helper` prints
# an access token. `aws configure get` reads one stored credential by name.
_CREDENTIAL_OPERATION_FLAGS = {
    ('kubectl', 'config', 'view'): frozenset({'--raw', '--flatten'}),
    ('config', 'view'): frozenset({'--raw', '--flatten'}),
}
_CREDENTIAL_OPERATIONS = (
    ('gcloud', 'config', 'config-helper'),
    ('config', 'config-helper'),
)
_CREDENTIAL_LOOKUPS = (
    ('aws', 'configure', 'get'),
    ('configure', 'get'),
)
# Output formats that read the template from a file the gate cannot see.
_TEMPLATE_FILE_OUTPUTS = ('go-template-file', 'template-file', 'jsonpath-file')
_TEMPLATE_FILE_REASON = 'it reads an output template from a file'
# Fixed strings: the credential one reaches a log line and must not echo the
# matched word (see `_credential_rejection`); the ssh one is shared by every
# path that decides the far side of ssh is not a read.
_CREDENTIAL_REASON = 'it returns a credential, key, secret or token'
_NOT_A_REMOTE_READ = 'it does not run a read-only remote command'


def _operation_returns_credential(operation: tuple, flag_names: set) -> bool:
    """Whether the operation hands back a credential through an option or operand."""
    for combo, flags in _CREDENTIAL_OPERATION_FLAGS.items():
        if operation[:len(combo)] == combo and flag_names & flags:
            return True
    if any(operation[:len(combo)] == combo for combo in _CREDENTIAL_OPERATIONS):
        return True
    for combo in _CREDENTIAL_LOOKUPS:
        if operation[:len(combo)] == combo:
            names = operation[len(combo):]
            return any(
                w in CREDENTIAL_WORDS
                for name in names for w in re.split(r'[-_.]+', name)
            )
    return False


def _option_values(tokens: list[str]):
    """Yield `(option, value)` for each option in one segment, lowercased name.

    `--flag=value`, `--flag value`, `-o value`, `-o=value` and the glued
    `-ovalue` all pair the same way. A boolean switch yields the token after
    it, which the callers only compare against fixed prefixes, so a wrong
    pairing can refuse but never clear.
    """
    for i, tok in enumerate(tokens):
        if not tok.startswith('-') or tok == '--':
            continue
        if not tok.startswith('--') and len(tok) > 2 and tok[2] != '=':
            name, value = tok[:2], tok[2:]
        else:
            name, has_value, value = tok.partition('=')
            if not has_value:
                value = tokens[i + 1] if i + 1 < len(tokens) else ''
        yield name.lower(), value


def _option_value_rejection(segments: list) -> str:
    """Why an option value makes the command a credential or file read, or ''.

    Option values never supply the verb, which is why `_operation_of` drops
    them. But `kubectl get --raw /api/v1/namespaces/x/secrets` names the same
    Secret that `kubectl get secret` refuses, so the credential words are
    matched against what `--raw` fetches, and `-o go-template-file=PATH`
    reads an arbitrary file as the template.
    """
    for tokens in segments:
        for name, value in _option_values(tokens):
            if name == '--raw':
                if set(_split_words(value.lower())) & CREDENTIAL_WORDS:
                    return _CREDENTIAL_REASON
            elif name in ('-o', '--output'):
                if value.lower().startswith(_TEMPLATE_FILE_OUTPUTS):
                    return _TEMPLATE_FILE_REASON
    return ''

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
    if executable in OPERAND_POSITIONAL_CLIS:
        # A subcommand group's operation runs one token longer, so the relaxed
        # scan has to reach it (`kubectl config` vs `config delete-context`).
        end = verb_index + 1
        if verb_index < len(operation) and operation[verb_index] in SUBCOMMAND_GROUPS:
            end += 1
        scanned = operation[:end]
    # Unknown CLI: scan everything, which over-blocks rather than under-blocks.
    else:
        scanned = operation
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
    return bool(_credential_rejection(facts, flag_names))


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


def _credential_rejection(facts: list, flag_names: set) -> str:
    """Why the command hands back credentials, or '' when it does not.

    Deliberately interpolates nothing: this reason reaches a log line, and
    feeding the credential vocabulary into one trips CodeQL's clear-text-logging
    rule for no gain, since the agent can already see its own command.
    """
    # Options that dump secrets gate the whole command.
    if flag_names & CREDENTIAL_FLAGS:
        return 'it passes an option that returns decrypted secrets'

    # A credential word in any segment blocks, unless *that* segment is exactly a
    # kubeconfig fetch. Checked per segment so one exempt segment can't clear a
    # credential read sitting in another.
    for f in facts:
        if f.credential_words & CREDENTIAL_WORDS and not _is_kubeconfig_fetch(f.operation):
            return _CREDENTIAL_REASON
        if _operation_returns_credential(f.operation, flag_names):
            return _CREDENTIAL_REASON

    all_words = set().union(*(f.words for f in facts))
    if any(all(part in all_words for part in combo) for combo in CREDENTIAL_READS):
        return 'it returns usable credentials'
    return ''


def _write_rejection(facts: list) -> str:
    """Why a write verb blocks the command, or '' when none is present."""
    for f in facts:
        if not f.write_verb:
            continue
        # Name the leading word too. Shape cannot tell a real `delete-log-group`
        # from a resource named `delete-me-vm`, so this is what makes a misparse
        # self-evident instead of looking like a genuine delete.
        verb = _verb_of(f.write_verb)
        if verb == f.write_verb:
            return "'%s' is a write operation" % f.write_verb
        return ("'%s' is a write operation (it leads with '%s')"
                % (f.write_verb, verb))
    return ''


def _pipeline_rejection(facts: list) -> str:
    """Why the command's shape blocks it: a wrapper, or a non-filter downstream."""
    # A wrapper's argument is the command that really runs, so what was
    # classified above is not what executes.
    if facts[0].executable in COMMAND_WRAPPERS:
        return ("'%s' runs another command, so what would actually execute "
                "cannot be checked" % facts[0].executable)

    for f in facts[1:]:
        if f.executable not in DOWNSTREAM_FILTERS:
            return ("its output is piped into '%s', which is not a text filter"
                    % (f.executable or '?'))
    return ''


# Host diagnostics the cloud verb list doesn't know. A write verb anywhere in
# the remote command, or a redirect, still refuses them.
SHELL_READ_PROGRAMS = frozenset({
    'cat', 'head', 'tail', 'less', 'more', 'grep', 'egrep', 'fgrep', 'rg',
    'df', 'du', 'free', 'ps', 'top', 'htop', 'uptime', 'w', 'who', 'whoami',
    'hostname', 'uname', 'id', 'date', 'journalctl', 'dmesg', 'ls', 'wc',
    'sort', 'uniq', 'cut', 'tr', 'stat', 'file', 'ss', 'netstat', 'lsof',
    'ping', 'dig', 'nslookup', 'vmstat', 'iostat', 'mpstat', 'sar',
    'lsblk', 'lscpu', 'lsmem', 'nproc', 'ip',
    'last', 'lastlog', 'echo', 'printf',
})

# `docker pull` has no write verb, so the binary itself is not a read.
# Only the inspection subcommands are.
_CONTAINER_CLIS = frozenset({'docker', 'podman', 'crictl'})
_CONTAINER_READS = frozenset({
    'ps', 'logs', 'inspect', 'images', 'top', 'stats', 'version', 'info',
    'port', 'diff', 'history',
})

# A binary in SHELL_READ_PROGRAMS can still mutate through a flag or a
# positional (`journalctl --vacuum-time`, `ip route del`, `date -s`).
_SHELL_MUTATING_EXACT = {
    # GNU hostname -F / --file writes the hostname from a file.
    'hostname': frozenset({'-F', '--file'}),
    'journalctl': frozenset({'--rotate', '--flush', '--sync', '--relinquish'}),
    'dmesg': frozenset({'-c', '--clear', '--read-clear', '-n', '--console'}),
    'date': frozenset({'-s', '--set'}),
    'sort': frozenset({'-o', '--output', '--compress-program'}),
    'sar': frozenset({'-o'}),
    'rg': frozenset({'--pre'}),
    # `-K` is the short form the man page documents. `-k` stays for the old spelling.
    'ss': frozenset({'-k', '-K', '--kill'}),
    'less': frozenset({'-o', '--log-file'}),
    # Text filters that edit in place, or run a script the gate cannot read.
    'sed': frozenset({'-i', '--in-place', '-f', '--file'}),
    'awk': frozenset({'-i', '--include', '-f', '--file', '-l', '--load',
                      '-E', '--exec', '-W'}),
    'gawk': frozenset({'-i', '--include', '-f', '--file', '-l', '--load',
                       '-E', '--exec', '-W'}),
    'yq': frozenset({'-i', '--inplace', '-f', '--from-file'}),
}
# `--vacuum` also covers `--vacuum-time` / `--vacuum-size` / `--vacuum-files`.
_SHELL_MUTATING_PREFIX = {
    'journalctl': ('--vacuum',),
}
# The characters after these short options are a value, not more flags.
# `date -Iseconds` prints a timestamp. The `s` is not `date -s`.
_SHELL_ATTACHED_VALUE = {
    'date': frozenset({'-I'}),
    # `awk -F: '{print $1}'` and `awk -vi=1` are a separator and a variable.
    'awk': frozenset({'-F', '-v'}),
    'gawk': frozenset({'-F', '-v'}),
    'sed': frozenset({'-l'}),
}

# What a filter's script may not do: run a program, pipe to one, read a file
# or write one. The script is a quoted token, so an unquoted `|` or `>` would
# already have split the segment; these are the ones hiding inside the quotes.
# `>` is only a redirect after `print`; elsewhere it is a comparison.
_AWK_PROGRAM_RUNS_OR_WRITES = re.compile(
    r'system\s*\(|getline|\||@include|@load|@\w+\s*\(|\bprintf?\b[^;{}]*>'
)
_AWK_PROGRAM_REASON = 'its awk program runs a command, reads a file or writes one'
_SED_SCRIPT_REASON = 'its sed script runs a command or writes a file'
_AWKS = frozenset({'awk', 'gawk'})
# awk options that consume the next token, so `-F '|'` and `-v sep='|'` are a
# separator and a variable rather than the program.
_AWK_VALUE_FLAGS = frozenset({
    '-F', '--field-separator', '-v', '--assign', '-f', '--file', '-e',
    '--source', '-i', '--include', '-l', '--load', '-E', '--exec', '-W',
})
_AWK_PROGRAM_FLAGS = frozenset({'-e', '--source'})
# sed options that consume the next token.
_SED_VALUE_FLAGS = frozenset({'-e', '--expression', '-f', '--file', '-l', '--line-length'})
_SED_SCRIPT_FLAGS = frozenset({'-e', '--expression'})
# `del` / `change` / `append` are not write verbs, but they change routes.
_IP_WRITE_ACTIONS = frozenset({
    'del', 'delete', 'change', 'append', 'replace', 'save',
})

# These ssh options run local code, forward ports, or write a file. `-o` is
# not in the set: its keys are allow-listed instead.
_SSH_UNSAFE_VALUE_FLAGS = frozenset({
    '-D', '-E', '-F', '-I', '-L', '-O', '-R', '-S', '-w',
})
_SSH_SAFE_OPTION_KEYS = frozenset({
    'batchmode', 'stricthostkeychecking', 'userknownhostsfile', 'connecttimeout',
    'connectionattempts', 'serveraliveinterval', 'serveralivecountmax', 'loglevel',
    'port', 'user', 'identityfile', 'identitiesonly', 'preferredauthentications',
})
_SSH_UNSAFE_OPTION = (
    'it passes an ssh option that can run local code, forward ports or write files'
)
# ProxyCommand is a local shell. These characters run or redirect before ssh connects.
_NESTED_SHELL_SYNTAX = re.compile(r'[$`\\;&|<>(){}*?\[\]!#\n\r]')

# ssh options that consume the next argv token. Anything else starting with
# '-' is a boolean switch (`-n`, `-tt`).
_SSH_VALUE_FLAGS = frozenset({
    '-B', '-b', '-c', '-D', '-E', '-e', '-F', '-I', '-i', '-J', '-L', '-l',
    '-m', '-O', '-o', '-p', '-Q', '-R', '-S', '-W', '-w',
})
_SUDO_VALUE_FLAGS = frozenset({
    '-u', '-g', '-C', '-p', '-r', '-t', '-h', '-T',
})
# `sh -c` is the command that runs. The shell binary itself is not a read.
_SHELLS = frozenset({'sh', 'bash', 'dash', 'ksh', 'zsh', 'ash'})
# One hop of `user@host`, `host`, `host:22`, or a bracketed IPv6. A comma
# separates hops. Anything else (`$(id)`, spaces, quotes) is a local shell.
_JUMP_HOP = re.compile(
    r'^(?:[A-Za-z0-9._-]+@)?(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?$'
)


def _is_assignment(token: str) -> bool:
    return '=' in token and not token.startswith('/') and not token.startswith('-')


def _ssh_flag_kind(token: str) -> str:
    """'separate' when the next token is the value, 'glued' when it's attached."""
    if token in _SSH_VALUE_FLAGS:
        return 'separate'
    if len(token) > 2 and token[:2] in _SSH_VALUE_FLAGS:
        return 'glued'
    return ''


def _is_ambiguous_ssh_bundle(token: str) -> bool:
    """`-vo`: a value flag hidden behind a boolean letter shifts the destination."""
    return (
        len(token) > 2
        and token[0] == '-'
        and token[1] != '-'
        and ('-' + token[1]) not in _SSH_VALUE_FLAGS
        and any(('-' + char) in _SSH_VALUE_FLAGS for char in token[2:])
    )


def _ssh_o_rejection(value: str, depth: int) -> str:
    """Why an `-o` value is unsafe, or '' when the key is on the allow list.

    `ProxyCommand` is the jump-host rewrite. It is allowed only when the value
    is itself `ssh ... -W`, checked with the same option rules.
    """
    parts = re.split(r'[=\s]+', value.strip(), maxsplit=1)
    key = parts[0].lower()
    nested = parts[1] if len(parts) > 1 else ''
    # The value is not parsed as a shell except for ProxyCommand, and that one is.
    if _NESTED_SHELL_SYNTAX.search(nested):
        return _SSH_UNSAFE_OPTION
    if key == 'proxycommand':
        try:
            nested_tokens = shlex.split(nested)
        except ValueError:
            return _SSH_UNSAFE_OPTION
        if _ssh_invocation_rejection(nested_tokens, depth + 1, jump_forward=True):
            return _SSH_UNSAFE_OPTION
        return ''
    if key not in _SSH_SAFE_OPTION_KEYS:
        return _SSH_UNSAFE_OPTION
    return ''


def _shell_flag_mutates(executable: str, flag: str) -> bool:
    """Whether *flag* makes an otherwise read-only host tool write or exec."""
    exact = _SHELL_MUTATING_EXACT.get(executable, ())
    prefixes = _SHELL_MUTATING_PREFIX.get(executable, ())
    if flag in exact or any(flag == p or flag.startswith(p + '-') for p in prefixes):
        return True
    # getopt accepts any unambiguous prefix of a long option, so `sort --outp`
    # is `sort --output`. An ambiguous prefix is refused rather than guessed at.
    if flag.startswith('--') and len(flag) > 2:
        if any(p.startswith('--') and p.startswith(flag) for p in exact):
            return True
    attached = _SHELL_ATTACHED_VALUE.get(executable, ())
    # `date -Iseconds` is one option plus a format, not a bundle that contains `-s`.
    if any(flag == prefix or flag.startswith(prefix) for prefix in attached):
        return False
    # `sort -ofile` glues the path on. `sort -uo` hides the letter in a bundle.
    if flag.startswith('-') and not flag.startswith('--'):
        letters = flag[1:]
        return any(len(pattern) == 2 and pattern[1] in letters for pattern in exact)
    return False


def _unwrap_sudo(command: str) -> str:
    """Drop one leading sudo/doas so the remote verb is what actually runs.

    `sudo journalctl` is a read. `sudo reboot` is a write. A bare `sudo -i`
    opens a shell and stays refused.
    """
    # Re-joining quotes `;`, `|` and `$(`, so the remote shell would still
    # run them while this check saw one literal command.
    if any(c in command for c in _CONTROL_CHARS + '\r\n'):
        return command
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if not argv or argv[0] not in ('sudo', 'doas'):
        return command
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == '--':
            return shlex.join(argv[i + 1:])
        if tok in _SUDO_VALUE_FLAGS:
            i += 2
            continue
        # Boolean sudo switches (`-n`, `-E`). `-i` with nothing after it is a login shell.
        if tok.startswith('-'):
            i += 1
            continue
        return shlex.join(argv[i:])
    return ''


def jump_target_is_host(spec: str) -> bool:
    """True when every `-J` hop is only a user, host, and port."""
    hops = [hop for hop in spec.split(',') if hop]
    return bool(hops) and all(_JUMP_HOP.match(hop) for hop in hops)


def _skip_delimited(script: str, i: int, delim: str) -> int:
    """Index just past the `delim` that closes the one at *i*, honouring `\\`."""
    i += 1
    while i < len(script):
        if script[i] == '\\':
            i += 2
            continue
        if script[i] == delim:
            return i + 1
        i += 1
    return i


def _strip_sed_address(piece: str) -> str:
    """One sed command with its address (`1,20`, `/re/`, `$!`) removed."""
    s = piece.lstrip()
    i = 0
    while i < len(s):
        c = s[i]
        if c.isdigit() or c in '$,~+!' or c.isspace():
            i += 1
        elif c == '/':
            i = _skip_delimited(s, i, '/')
            i = _skip_regex_modifiers(s, i)
        # `\cREGEXc` is a regex address with a custom delimiter.
        elif c == '\\' and i + 1 < len(s):
            i = _skip_delimited(s, i + 1, s[i + 1])
            i = _skip_regex_modifiers(s, i)
        else:
            break
    return s[i:]


def _skip_regex_modifiers(s: str, i: int) -> int:
    """Past the `I` / `M` modifiers a regex address may carry (`/re/Ip`)."""
    while i < len(s) and s[i] in 'IM':
        i += 1
    return i


def _sed_script_mutates(script: str) -> bool:
    """Whether a sed script runs a program (`e`) or writes a file (`w`, `W`).

    Both exist as commands and as flags of `s`. Everything else sed can do
    only changes the text it prints. The script is walked command by
    command, so a `;` inside `s/a;b/c/` is part of the pattern, not a
    separator.
    """
    return any(
        cmd in 'ewW' or (cmd == 's' and any(flag in 'ew' for flag in flags))
        for cmd, flags in _sed_commands(script)
    )


def _sed_segment_end(script: str, i: int, stops: str) -> int:
    """Index of the first character of *stops* at or after *i*, or the end."""
    while i < len(script) and script[i] not in stops:
        i += 1
    return i


def _sed_parts_end(script: str, i: int) -> int:
    """Index just past the third delimiter of the `s` or `y` command at *i*."""
    if i + 1 >= len(script):
        return len(script)
    delim = script[i + 1]
    end = i + 1
    for _ in range(2):
        end = _skip_delimited(script, end, delim) - 1
    return end + 1


def _sed_commands(script: str):
    """Yield `(command, flags)` for each command in a sed script, in order.

    Only `s` carries flags. Addresses are stripped, `s`/`y` are skipped by
    their delimiters so a `;` inside the pattern is not a separator, `a`/`i`/`c`
    text and `#` comments run to the end of the line, and a block's commands
    follow its brace.
    """
    n = len(script)
    i = 0
    while i < n:
        if script[i] in ';\n{} \t':
            i += 1
            continue
        rest = _strip_sed_address(script[i:])
        i = n - len(rest)
        if i >= n:
            return
        cmd = script[i]
        if cmd in '{}':
            i += 1
        elif cmd in 'sy':
            end = _sed_parts_end(script, i)
            flags_end = _sed_segment_end(script, end, ';\n}')
            yield cmd, script[end:flags_end]
            i = flags_end
        elif cmd in 'aic#':
            yield cmd, ''
            i = _sed_segment_end(script, i, '\n')
        else:
            yield cmd, ''
            i = _sed_segment_end(script, i + 1, ';\n{}')


def _filter_programs(tokens: list[str], value_flags: frozenset,
                     program_flags: frozenset, short_program_flag: str) -> list[str]:
    """The program text a sed or awk segment runs.

    Every program option (`-e` / `--source`) contributes one; with none given
    the program is the first positional. Options that take a value consume
    it, so `-F '|'` or `-l 5` is never mistaken for the program, and a glued
    `-eSCRIPT` or bundled `-ne SCRIPT` keeps its script.
    """
    programs: list[str] = []
    positionals: list[str] = []
    i = 0
    while i < len(tokens) and _is_assignment(tokens[i]):
        i += 1
    i += 1  # the filter binary itself
    while i < len(tokens):
        tok = tokens[i]
        if tok == '--':
            positionals.extend(tokens[i + 1:])
            break
        if not tok.startswith('-') or tok == '-':
            positionals.append(tok)
            i += 1
            continue
        program, consumed = _program_in_option(
            tok, tokens[i + 1:i + 2], value_flags, program_flags, short_program_flag,
        )
        if program is not None:
            programs.append(program)
        i += 1 + consumed
    return programs or positionals[:1]


def _program_in_option(tok: str, following: list[str], value_flags: frozenset,
                       program_flags: frozenset, short_program_flag: str,
                       ) -> tuple[Optional[str], int]:
    """The program an option token carries, and how many later tokens it took.

    `-e PROG` and `--source=PROG` carry it; any other value option (`-F '|'`,
    `-l 5`) consumes its token without yielding one. A short option glued to
    its value (`-F:`, `-vsep=1`, `-l5`, `-eSCRIPT`) carries it only if it is
    the program option. A bundle ending in the program letter (`-ne`) takes
    the next token.
    """
    next_token = (following[0] if following else None), (1 if following else 0)
    if tok in value_flags:
        return next_token if tok in program_flags else (None, next_token[1])
    if tok.startswith('--'):
        name, has_value, value = tok.partition('=')
        return (value if name in program_flags and has_value else None), 0
    if tok[:2] in value_flags:
        return (tok[2:] if tok[:2] in program_flags else None), 0
    return _program_in_bundle(tok[1:], short_program_flag, next_token)


def _program_in_bundle(letters: str, short_program_flag: str,
                       next_token: tuple[Optional[str], int]) -> tuple[Optional[str], int]:
    """The program a short-option bundle carries: glued after the letter, or next."""
    if short_program_flag not in letters:
        return None, 0
    glued = letters.split(short_program_flag, 1)[1]
    return (glued, 0) if glued else next_token


def _filter_script_rejection(executable: str, tokens: list[str]) -> str:
    """Why a text filter's script is not a read, or '' when it only filters.

    A filter was cleared by its name alone, so `awk '{system("...")}'` and
    `sed 'e ...'` ran a program on the far side of ssh as a read.
    """
    if executable in _AWKS:
        programs = _filter_programs(tokens, _AWK_VALUE_FLAGS, _AWK_PROGRAM_FLAGS, 'e')
        if any(_AWK_PROGRAM_RUNS_OR_WRITES.search(p) for p in programs):
            return _AWK_PROGRAM_REASON
    elif executable == 'sed':
        scripts = _filter_programs(tokens, _SED_VALUE_FLAGS, _SED_SCRIPT_FLAGS, 'e')
        if any(_sed_script_mutates(s) for s in scripts):
            return _SED_SCRIPT_REASON
    return ''


def _segment_mutation_reason(tokens: list[str],
                             fact: Optional[SegmentFacts] = None) -> str:
    """Why a host tool writes through a flag, a script or a positional, not a verb."""
    if fact is None:
        fact = analyze_segment(tokens)
    exe = fact.executable or ''
    # Short options stay cased: `hostname -F` writes, `hostname -f` only prints.
    flags = [
        (t.split('=', 1)[0].lower() if t.startswith('--') else t.split('=', 1)[0])
        for t in tokens if t.startswith('-')
    ]
    for flag in flags:
        if _shell_flag_mutates(exe, flag):
            return "'%s %s' writes a file or runs a program" % (exe, flag)
    reason = _filter_script_rejection(exe, tokens)
    if reason:
        return reason
    # `hostname` with a name renames the host. A bare `hostname` only prints it.
    if exe == 'hostname' and len(fact.operation) > 1:
        return 'it renames the host'
    # `uniq in out` writes the second file. One operand is a read.
    if exe == 'uniq' and len(fact.operation) > 2:
        return "'uniq' with two operands writes the second one"
    # `ip route del` is not a write verb. Any mutating action blocks.
    if exe == 'ip' and any(tok in _IP_WRITE_ACTIONS for tok in fact.operation[1:]):
        return "'ip' changes the network configuration"
    return ''


def _segment_mutates(tokens: list[str], fact: Optional[SegmentFacts] = None) -> bool:
    """Whether a host tool writes through a flag, a script or a positional, not a verb."""
    return bool(_segment_mutation_reason(tokens, fact))


def _shell_c_script(tokens: list[str]) -> Optional[str]:
    """The script of `sh -c`, or None when this segment is not a shell.

    An empty string means the shell runs something this check cannot see
    (`sh script.sh`, or `-c` with no script).
    """
    i = 0
    while i < len(tokens) and _is_assignment(tokens[i]):
        i += 1
    if i >= len(tokens):
        return None
    exe = tokens[i].rsplit('/', 1)[-1].lower()
    if exe not in _SHELLS:
        return None
    i += 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ('-c', '--command'):
            return tokens[i + 1] if i + 1 < len(tokens) else ''
        # `-lc` is login plus -c. A glued `-cCMD` keeps the script on this token.
        if tok.startswith('-') and not tok.startswith('--') and 'c' in tok[1:]:
            glued = tok.split('c', 1)[1]
            return glued if glued else (tokens[i + 1] if i + 1 < len(tokens) else '')
        if tok.startswith('-'):
            i += 1
            continue
        return ''
    return ''


def _is_shell_diagnostic(command: str) -> bool:
    """Whether every program in *command* only reads."""
    segments, _reason = _split_segments_or_reason(command)
    if not segments:
        return False
    facts = [analyze_segment(seg) for seg in segments]
    if any(f.write_verb for f in facts):
        return False
    if facts[0].executable in COMMAND_WRAPPERS:
        return False
    if any(f.executable not in DOWNSTREAM_FILTERS for f in facts[1:]):
        return False
    # Container CLIs mutate with subcommands that are not write verbs (`pull`).
    if facts[0].executable in _CONTAINER_CLIS:
        op = facts[0].operation
        return len(op) >= 2 and op[1] in _CONTAINER_READS
    # `journalctl --vacuum-time`, `sort -o out` and `sed 'e cmd'` are writes
    # with no write verb, in the first segment or in a downstream filter.
    if any(_segment_mutates(seg, fact) for seg, fact in zip(segments, facts)):
        return False
    return facts[0].executable in SHELL_READ_PROGRAMS


def _one_segment_rejection(tokens: list[str], depth: int) -> str:
    """Why this one segment isn't a read, or '' when it is."""
    one = shlex.join(tokens)
    reason = describe_rejection(one, _ssh_depth=depth)
    # A recognised read verb used to clear the rest of the line. `sort -o`
    # is still a write, and it has no write verb for that check to see.
    fact = analyze_segment(tokens)
    mutation = _segment_mutation_reason(tokens, fact)
    if mutation:
        return mutation
    if not reason:
        return ''
    # Host tools and text filters have no cloud verb. Mutating flags already returned.
    if reason == 'no recognised read verb was found in it' and (
        _is_shell_diagnostic(one) or fact.executable in DOWNSTREAM_FILTERS
    ):
        return ''
    return reason


def _remote_command_rejection(remote: str, depth: int, shell_depth: int = 0) -> str:
    """Why a command run on the far side of ssh isn't a read."""
    # A nested `sh -c` can hide another `sh -c`. Stop rather than recurse forever.
    if shell_depth > 4:
        return _NOT_A_REMOTE_READ
    segments, parse_reason = _split_segments_or_reason(remote)
    if not segments:
        return parse_reason
    for seg in segments:
        script = _shell_c_script(seg)
        # The script is what runs. The shell binary itself is not a read verb.
        if script is not None:
            if not script.strip():
                return _NOT_A_REMOTE_READ
            reason = _remote_command_rejection(script, depth, shell_depth + 1)
            if reason:
                return reason
            continue
        reason = _one_segment_rejection(seg, depth)
        if reason:
            return reason
    return ''


def _ssh_invocation_rejection(
    tokens: list[str], depth: int, *, jump_forward: bool,
) -> str:
    """Why this ssh argv isn't allowed, or '' when it is.

    *jump_forward* allows `ssh -W` (the jump rewrite) and rejects a remote
    command. Otherwise `-W` is a tunnel and the remote command must be a read.
    """
    # A ProxyCommand that nests another ProxyCommand is how a local command hides.
    if depth >= 2:
        return _SSH_UNSAFE_OPTION

    i = 0
    while i < len(tokens) and _is_assignment(tokens[i]):
        i += 1
    if i >= len(tokens) or tokens[i].rsplit('/', 1)[-1].lower() != 'ssh':
        return 'no recognised read verb was found in it'
    i += 1

    tunnel = False
    saw_forward = False
    saw_destination = False
    remote_argv: list[str] = []
    while i < len(tokens):
        tok = tokens[i]
        # `--` ends ssh's own options; the rest is the remote command.
        if tok == '--':
            remote_argv = tokens[i + 1:]
            break
        kind = _ssh_flag_kind(tok)
        # `-vo` hides a value flag behind a boolean letter, so the next token
        # would be read as the destination instead of the option value.
        if _is_ambiguous_ssh_bundle(tok) or tok[:2] in _SSH_UNSAFE_VALUE_FLAGS:
            return _SSH_UNSAFE_OPTION
        # `-N` / `-s` never run a diagnostic. `-W` is a tunnel on the outer
        # ssh and the stdio forward inside a jump ProxyCommand.
        if tok in ('-N', '-s') or (kind == 'glued' and tok[:2] == '-s'):
            tunnel = True
        if tok == '-W' or tok[:2] == '-W':
            forward_value = tokens[i + 1] if kind == 'separate' and i + 1 < len(tokens) else tok[2:]
            if any(c in forward_value for c in _CONTROL_CHARS):
                return _SSH_UNSAFE_OPTION
            if jump_forward:
                saw_forward = True
            else:
                tunnel = True
        if kind == 'separate':
            if i + 1 >= len(tokens):
                return 'no recognised read verb was found in it'
            if tok == '-o':
                reason = _ssh_o_rejection(tokens[i + 1], depth)
                if reason:
                    return reason
            # `-J` is copied into a shell-parsed ProxyCommand. Only a host is safe.
            if tok == '-J' and not jump_target_is_host(tokens[i + 1]):
                return _SSH_UNSAFE_OPTION
            i += 2
            continue
        if kind == 'glued':
            if tok[:2] == '-o':
                reason = _ssh_o_rejection(tok[2:], depth)
                if reason:
                    return reason
            if tok[:2] == '-J' and not jump_target_is_host(tok[2:]):
                return _SSH_UNSAFE_OPTION
            i += 1
            continue
        if tok.startswith('-') and tok != '-':
            i += 1
            continue
        # First non-option is the destination. The jump rewrite puts `-p` after
        # it, so keep scanning options; a second positional is a remote command.
        if jump_forward and saw_destination:
            return _SSH_UNSAFE_OPTION
        if jump_forward:
            # `$(id>/tmp/x)` is one token, and ProxyCommand runs it in a local shell.
            if not jump_target_is_host(tok):
                return _SSH_UNSAFE_OPTION
            saw_destination = True
            i += 1
            continue
        # The local shell expands this token before ssh starts. Only a host is safe.
        if not jump_target_is_host(tok):
            return _SSH_UNSAFE_OPTION
        remote_argv = tokens[i + 1:]
        saw_destination = True
        break
    else:
        remote_argv = []

    if jump_forward:
        # A jump ProxyCommand is `ssh -W %h:%p bastion`, not a remote shell.
        if not saw_forward or not saw_destination or tunnel or remote_argv:
            return _SSH_UNSAFE_OPTION
        return ''

    if tunnel:
        return _NOT_A_REMOTE_READ
    if not saw_destination or not remote_argv:
        return 'it opens an interactive shell'

    # ssh joins the remote argv and hands it to the remote shell. A quoted
    # pipeline (`ssh host "df | grep x"`) is one argv token and must be
    # re-parsed as a shell command, not as a single literal.
    remote = _unwrap_sudo(' '.join(remote_argv))
    if not remote.strip():
        return 'it opens an interactive shell'

    # The outer `-i` is already gone. A key path here is the remote command
    # reading it (`sort -i ~/.ssh/id_rsa`), not Aurora authenticating.
    from utils.security.signature_match import ssh_key_material_exposed
    if ssh_key_material_exposed(remote, allow_identity_option=False):
        return _CREDENTIAL_REASON

    # Every program in the remote command has to be a read, including a
    # write that follows a recognised read verb and a script inside `sh -c`.
    return _remote_command_rejection(remote, depth + 1)


def _ssh_remote_rejection(tokens: list[str], depth: int) -> str:
    """Why an ssh invocation isn't a read, or '' when the remote command is."""
    return _ssh_invocation_rejection(tokens, depth, jump_forward=False)


def describe_rejection(command: str, _ssh_depth: int = 0) -> str:
    """Why *command* is not read-only, as a clause, or '' when it is read-only.

    The agent sees this in the blocked tool's error, so it says which token
    caused the refusal: 'modifies infrastructure' alone is not enough to repair
    a command with.
    """
    segments, parse_reason = _split_segments_or_reason(command)
    if not segments:
        return parse_reason

    facts = [analyze_segment(seg) for seg in segments]

    # ssh has no read verb of its own. The remote command is what runs, and the
    # destination must not be scanned (`ssh update.example` is a hostname).
    if facts[0].executable == 'ssh':
        if _ssh_depth >= 2:
            return 'no recognised read verb was found in it'
        chain = _pipeline_rejection(facts)
        if chain:
            return chain
        # `sort` is a text filter, and `sort -o` still writes a local file.
        for seg in segments[1:]:
            reason = _one_segment_rejection(seg, _ssh_depth)
            if reason:
                return reason
        return _ssh_remote_rejection(segments[0], _ssh_depth)

    # Flag names gate the command (`--with-decryption`, `--dry-run`) but must
    # never provide a read-only verb, so they are collected separately.
    flag_names = {
        t.split('=', 1)[0].lower() for seg in segments for t in seg
        if t.startswith('-') and t != '--'
    }

    for reason in (_credential_rejection(facts, flag_names),
                   _option_value_rejection(segments),
                   _write_rejection(facts),
                   _pipeline_rejection(facts)):
        if reason:
            return reason

    # A downstream filter is a text filter by name, and `sed -i` or
    # `awk '{system("...")}'` still writes or runs a program.
    for seg, fact in zip(segments[1:], facts[1:]):
        reason = _segment_mutation_reason(seg, fact)
        if reason:
            return reason

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


def is_diagnostic_ssh(command: str) -> bool:
    """Whether *command* is ssh running a read-only remote command."""
    segments, _reason = _split_segments_or_reason(command)
    if not segments:
        return False
    if analyze_segment(segments[0]).executable != 'ssh':
        return False
    return describe_rejection(command) == ''
