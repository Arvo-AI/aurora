---
name: tailscale
id: tailscale
description: "Tailscale mesh VPN integration for managing devices, SSH access, auth keys, ACLs, and DNS across your tailnet"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
  - tailscale_ssh
index: "Tailscale — device management, SSH access, auth keys, ACLs, DNS/routes"
rca_priority: 10
allowed-tools: cloud_exec, tailscale_ssh
metadata:
  author: aurora
  version: "1.0"
---

# Tailscale Integration

## Overview
Tailscale is a mesh VPN/network provider. It connects your devices into a secure private network called a 'tailnet'.
Unlike cloud providers (GCP, AWS, Azure), Tailscale doesn't provision infrastructure - it networks existing devices.

## Instructions

### DEVICE MANAGEMENT
- List all devices: `cloud_exec('tailscale', 'device list')`
- Get device details: `cloud_exec('tailscale', 'device get <DEVICE_ID>')`
- Authorize a device: `cloud_exec('tailscale', 'device authorize <DEVICE_ID>')`
- Delete a device: `cloud_exec('tailscale', 'device delete <DEVICE_ID>')`
- Set device tags: `cloud_exec('tailscale', 'device tag <DEVICE_ID> tag:server')`

### SSH ACCESS (execute commands on devices)
- Run command on device: `tailscale_ssh('hostname', 'command', 'user')`
- Example - check uptime: `tailscale_ssh('myserver', 'uptime', 'root')`
- Example - docker status: `tailscale_ssh('web-prod', 'docker ps', 'admin')`
- Example - disk usage: `tailscale_ssh('database-1', 'df -h', 'ubuntu')`
- SETUP REQUIRED: User must add Aurora's SSH public key to target devices
  (Get key from Settings > Tailscale > SSH Setup)
- Targets must have SSH server running (Linux: sshd, macOS: Remote Login)
- If 'Permission denied' error: remind user to add Aurora's SSH key to the device

### AUTH KEYS (for adding devices programmatically)
- List auth keys: `cloud_exec('tailscale', 'key list')`
- Create auth key: `cloud_exec('tailscale', 'key create --ephemeral --reusable --tags tag:server')`
- Delete auth key: `cloud_exec('tailscale', 'key delete <KEY_ID>')`

### ACL (Access Control Lists)
- Get current ACL: `cloud_exec('tailscale', 'acl get')`
- Update ACL: `cloud_exec('tailscale', 'acl set <ACL_JSON>')`

### DNS & NETWORK
- Get DNS settings: `cloud_exec('tailscale', 'dns get')`
- List subnet routes: `cloud_exec('tailscale', 'routes list')`

### KEY CONCEPTS
- **Tailnet**: Your private Tailscale network
- **Device**: Any machine connected to your tailnet
- **Tags**: Labels for devices (must start with 'tag:' prefix)
- **Auth Key**: Token to add devices programmatically
- **ACL**: Access Control List for device communication

### CRITICAL RULES
- Use cloud_exec('tailscale', ...) for device/key/ACL management
- Use tailscale_ssh('hostname', 'command', 'user') to run commands on devices
- Tags must start with 'tag:' prefix (e.g., tag:server)
- Auth key values are only shown once at creation
- Tailscale does NOT provision infrastructure
