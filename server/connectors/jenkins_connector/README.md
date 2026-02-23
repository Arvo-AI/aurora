# Jenkins Connector

Read-only connector for Jenkins CI/CD servers.

## Authentication

Uses **HTTP Basic Auth** with a Jenkins username and API token.

## Setup

1. Log in to your Jenkins instance
2. Navigate to **People → Your User → Configure → API Token**
3. Click **Add new Token**, give it a name, and click **Generate**
4. Copy the token and enter it in Aurora along with the Jenkins URL and your username

## Permissions

The connector is read-only. It accesses:
- Server info and version
- Job listings and details
- Build history and console output
- Build queue
- Node/agent status

No builds are triggered and no configuration is modified.
