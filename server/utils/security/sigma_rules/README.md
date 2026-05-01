# SigmaHQ Vendored Rules

## Source

- Repository: https://github.com/SigmaHQ/sigma
- Release: [r2026-01-01](https://github.com/SigmaHQ/sigma/releases/tag/r2026-01-01)
- Commit SHA: see VERSION file for pinned release tag
- Path: `rules/linux/process_creation/`
- Filter: `level: high` or `critical`, detection fields limited to `CommandLine`, `Image`, `OriginalFileName`

## License

These rule files are vendored from the SigmaHQ project and are licensed under the
[Detection Rule License (DRL) 1.1](https://github.com/SigmaHQ/sigma/blob/master/LICENSE.Detection.Rules.md).

Original authorship is preserved in each YAML file (`author`, `date`, `id` fields).

## Vendored

- Date: 2026-04-24
- Release tag: `r2026-01-01`
- Source path: `rules/linux/process_creation/`
- 37 rules selected (all `level: high` or `critical` with supported detection fields)

### Vendored files

```
proc_creation_lnx_auditctl_clear_rules.yml
proc_creation_lnx_av_kaspersky_av_disabled.yml
proc_creation_lnx_awk_shell_spawn.yml
proc_creation_lnx_capsh_shell_invocation.yml
proc_creation_lnx_clear_syslog.yml
proc_creation_lnx_cp_passwd_or_shadow_tmp.yml
proc_creation_lnx_crypto_mining.yml
proc_creation_lnx_curl_wget_exec_tmp.yml
proc_creation_lnx_env_shell_invocation.yml
proc_creation_lnx_esxcli_permission_change_admin.yml
proc_creation_lnx_find_shell_execution.yml
proc_creation_lnx_flock_shell_execution.yml
proc_creation_lnx_gcc_shell_execution.yml
proc_creation_lnx_git_shell_execution.yml
proc_creation_lnx_malware_gobrat_grep_payload_discovery.yml
proc_creation_lnx_netcat_reverse_shell.yml
proc_creation_lnx_nice_shell_execution.yml
proc_creation_lnx_nohup_susp_execution.yml
proc_creation_lnx_omigod_scx_runasprovider_executescript.yml
proc_creation_lnx_omigod_scx_runasprovider_executeshellcommand.yml
proc_creation_lnx_perl_reverse_shell.yml
proc_creation_lnx_php_reverse_shell.yml
proc_creation_lnx_python_reverse_shell.yml
proc_creation_lnx_python_shell_os_system.yml
proc_creation_lnx_rsync_shell_execution.yml
proc_creation_lnx_rsync_shell_spawn.yml
proc_creation_lnx_ssh_shell_execution.yml
proc_creation_lnx_susp_history_delete.yml
proc_creation_lnx_susp_hktl_execution.yml
proc_creation_lnx_susp_java_children.yml
proc_creation_lnx_susp_recon_indicators.yml
proc_creation_lnx_susp_shell_child_process_from_parent_tmp_folder.yml
proc_creation_lnx_systemctl_mask_power_settings.yml
proc_creation_lnx_triple_cross_rootkit_execve_hijack.yml
proc_creation_lnx_triple_cross_rootkit_install.yml
proc_creation_lnx_vim_shell_execution.yml
proc_creation_lnx_webshell_detection.yml
```

## Updating

To update to a newer SigmaHQ release:

1. Download the new release from https://github.com/SigmaHQ/sigma/releases
2. Filter `rules/linux/process_creation/` for `level: high` or `critical`
3. Replace the `.yml` files in this directory
4. Update the `VERSION` file with the new release tag
5. Run `pytest tests/architectural/test_sigma_canary.py` to verify no false positives
