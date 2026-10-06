---
id: sso
title: Single Sign-On (SAML)
sidebar_label: Single Sign-On (SAML)
---

# Single Sign-On (SAML)

Aurora supports SAML 2.0 single sign-on with any standards-compliant identity provider (IdP): Microsoft Entra ID, Okta, Google Workspace, OneLogin, JumpCloud, Keycloak and others. SSO is configured per organization by an org admin under **Settings → Single Sign-On**.

## How it works

1. A member clicks **Sign in with SSO** and enters their work email.
2. Aurora finds the organization that verified that email's domain and redirects to its IdP.
3. The IdP posts a signed response back to that organization's Assertion Consumer Service (ACS) URL.
4. Aurora verifies the signature with that organization's certificate, then signs the member in. First-time members are created automatically with the organization's default role.

The organization a member joins is decided by **which IdP certificate verified the response**, never by the email they typed. The email's domain must also be verified by that same organization.

Only SP-initiated logins are accepted. To launch Aurora from an IdP dashboard tile (for example Entra's My Apps), set the application's **Sign on URL** to Aurora's Sign on URL; the tile then starts an SP-initiated login.

## Setup

### 1. Register Aurora in your IdP

Create a custom SAML application and copy these values from **Settings → Single Sign-On**:

| Aurora field | Entra ID | Okta |
|---|---|---|
| Identifier (Entity ID) | Identifier (Entity ID) | Audience URI (SP Entity ID) |
| Reply URL (ACS URL) | Reply URL | Single sign-on URL |
| Sign on URL | Sign on URL | — |

Assertions must be signed (the default in Entra ID, Okta and Google Workspace). Aurora reads these claims:

- **Email**: the `emailaddress` / `email` / `mail` attribute, or the NameID when it's an email.
- **User ID**: Entra's `objectidentifier` claim when present (immutable), otherwise the NameID.
- **Name**: `displayname` / `name`, or given name + surname.

In Entra ID, assign the users or groups who should have access to the application.

### 2. Verify your email domains

Add each domain members sign in with. Aurora shows a DNS TXT record to create:

```
_aurora-sso.example.com  TXT  "aurora-sso-verification=<token>"
```

Click **Verify** once the record has propagated. A domain can only be verified by one organization.

DNS proof is only required when `SSO_ENFORCE_DOMAIN_VERIFICATION=true`; otherwise added domains are trusted immediately. Enable it on any deployment where organizations don't trust each other (multi-tenant, or internet-facing with open sign-up): without it, any org admin can claim any domain and route its users' sign-ins to their own IdP.

### 3. Connect your IdP

Paste the IdP's federation metadata XML (Entra: **SAML Certificates → Federation Metadata XML**), or enter the IdP entity ID, SSO URL and signing certificate manually. Choose the role new members get, turn on **Enable SSO**, and save.

## Requiring SSO

**Require SSO** blocks password sign-in for members whose email is on a verified domain. Admins keep password access as a break-glass path in case the IdP is unavailable.

## Account linking rules

| Situation | Result |
|---|---|
| Returning SSO member | Matched by the IdP's user ID, so email renames don't break login |
| Existing password member of the same org | Linked to their SSO identity on first SSO login |
| Email already belongs to another Aurora org | Refused; accounts are never moved between orgs |
| Email domain not verified by the org | Refused |

## Requirements

- `NEXT_PUBLIC_BACKEND_URL` must be the backend's public URL as browsers see it; it is used to build the Entity ID and ACS URL, and SAML responses are validated against it.
- The ACS relies on a `SameSite=None; Secure` cookie, so the backend must be served over HTTPS to use any hosted IdP (Entra ID, Okta, …). Over plain HTTP, only an IdP on the same host as the backend (e.g. a local Keycloak for development) is accepted.

## Not yet supported

Single Logout (SLO), SCIM provisioning, and mapping IdP groups to Aurora roles.
