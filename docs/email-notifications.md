# Email Notifications

Aurora sends email notifications for incident investigations and action completions. This requires an SMTP provider.

## Setup

Add the following environment variables to your server configuration:

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your-smtp-username
SMTP_PASSWORD=your-smtp-password
SMTP_FROM_EMAIL=notifications@yourdomain.com
SMTP_FROM_NAME=Aurora
FRONTEND_URL=https://your-aurora-domain.com
```

| Variable | Required | Description |
|----------|----------|-------------|
| `SMTP_HOST` | Yes | SMTP server hostname |
| `SMTP_PORT` | No | SMTP port (default: 587) |
| `SMTP_USER` | Yes | SMTP authentication username |
| `SMTP_PASSWORD` | Yes | SMTP authentication password |
| `SMTP_FROM_EMAIL` | Yes | Sender email address |
| `SMTP_FROM_NAME` | No | Sender display name (default: "Aurora SRE") |
| `FRONTEND_URL` | No | Your Aurora frontend URL for email links (default: http://localhost:3000) |

## Supported Providers

Any SMTP provider works. Common options:

- **SendGrid** — `smtp.sendgrid.net`, port 587, username `apikey`, password is your API key
- **AWS SES** — `email-smtp.<region>.amazonaws.com`, port 587, use IAM SMTP credentials
- **Mailgun** — `smtp.mailgun.org`, port 587
- **Postmark** — `smtp.postmarkapp.com`, port 587
- **Self-hosted** — Any SMTP server supporting STARTTLS on port 587

## Email Types

Aurora sends four types of notification emails:

1. **Investigation Started** — Triggered when a new incident is created and RCA begins
2. **RCA Complete** — Sent when root cause analysis finishes with a summary
3. **Email Verification** — One-time code to confirm a user's notification email address
4. **Action Completed/Failed** — Sent when an automated action finishes (success or failure with error details)

## Enabling Notifications

Users enable email notifications in **Settings > Notifications** within the Aurora UI. They must verify their email address before notifications are sent.
