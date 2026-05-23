"""Email service for sending notifications via SMTP (SendGrid)."""

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class EmailService:
    """SMTP-based email service for Aurora notifications."""

    SEVERITY_COLORS = {
        'critical': {'bg': '#ef4444', 'text': '#ffffff', 'glow': 'rgba(239, 68, 68, 0.15)'},
        'high': {'bg': '#ef4444', 'text': '#ffffff', 'glow': 'rgba(239, 68, 68, 0.15)'},
        'error': {'bg': '#ef4444', 'text': '#ffffff', 'glow': 'rgba(239, 68, 68, 0.15)'},
        'warning': {'bg': '#f59e0b', 'text': '#ffffff', 'glow': 'rgba(245, 158, 11, 0.15)'},
        'medium': {'bg': '#f59e0b', 'text': '#ffffff', 'glow': 'rgba(245, 158, 11, 0.15)'},
        'info': {'bg': '#6b7280', 'text': '#ffffff', 'glow': 'rgba(107, 114, 128, 0.15)'},
        'low': {'bg': '#6b7280', 'text': '#ffffff', 'glow': 'rgba(107, 114, 128, 0.15)'},
    }
    DEFAULT_SEVERITY_COLOR = {'bg': '#6b7280', 'text': '#ffffff', 'glow': 'rgba(107, 114, 128, 0.15)'}

    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.from_email = os.getenv("SMTP_FROM_EMAIL")
        self.from_name = os.getenv("SMTP_FROM_NAME", "Aurora SRE")
        self.frontend_url = os.getenv("FRONTEND_URL", "https://aurora-ai.net")

        for env_var, attr in [("SMTP_HOST", "smtp_host"), ("SMTP_USER", "smtp_user"), ("SMTP_PASSWORD", "smtp_password")]:
            if not getattr(self, attr):
                raise ValueError(f"EmailService configuration incomplete. Missing required environment variable: {env_var}")

    def _send_email(self, to_email: str, subject: str, html_body: str, text_body: str) -> bool:
        try:
            msg = MIMEMultipart('alternative')
            msg['From'] = f"{self.from_name} <{self.from_email}>"
            msg['To'] = to_email
            msg['Subject'] = subject

            msg.attach(MIMEText(text_body, 'plain'))
            msg.attach(MIMEText(html_body, 'html'))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)

            logger.info(f"[EmailService] Email sent successfully to {to_email}: {subject}")
            return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"[EmailService] SMTP authentication failed: {e}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"[EmailService] SMTP error sending email: {e}")
            return False
        except Exception as e:
            logger.error(f"[EmailService] Unexpected error sending email: {e}")
            return False

    def _get_severity_color(self, severity: str) -> Dict[str, str]:
        return self.SEVERITY_COLORS.get(severity.lower(), self.DEFAULT_SEVERITY_COLOR)

    def _format_timestamp(self, timestamp) -> str:
        if isinstance(timestamp, datetime):
            return timestamp.strftime('%b %d, %Y at %H:%M UTC')
        return str(timestamp) if timestamp else 'just now'

    def _get_incident_url(self, incident_id: str) -> str:
        return f"{self.frontend_url}/incidents/{incident_id}"

    def _base_html(self, content: str, accent_color: str = '#000000') -> str:
        """Wrap content in the base email shell."""
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #0a0a0a; color: #ffffff;">
    <table role="presentation" style="width: 100%; border-collapse: collapse;">
        <tr>
            <td style="padding: 48px 20px;">
                <!-- Logo -->
                <table role="presentation" style="max-width: 600px; margin: 0 auto 24px auto;">
                    <tr>
                        <td style="text-align: center; padding-bottom: 32px;">
                            <div style="font-size: 18px; font-weight: 700; color: #ffffff; letter-spacing: 3px; text-transform: uppercase;">AURORA</div>
                            <div style="font-size: 10px; color: #525252; letter-spacing: 2px; margin-top: 4px; text-transform: uppercase;">Intelligent Incident Response</div>
                        </td>
                    </tr>
                </table>

                <!-- Main Card -->
                <table role="presentation" style="max-width: 600px; margin: 0 auto; background-color: #141414; border: 1px solid #262626; overflow: hidden;">
                    <!-- Accent Bar -->
                    <tr>
                        <td style="height: 3px; background: {accent_color};"></td>
                    </tr>
                    {content}
                </table>

                <!-- Footer -->
                <table role="presentation" style="max-width: 600px; margin: 24px auto 0 auto;">
                    <tr>
                        <td style="text-align: center; padding: 16px 0;">
                            <div style="font-size: 11px; color: #404040; letter-spacing: 0.5px;">
                                Aurora AI &bull; <a href="https://aurora-ai.net" style="color: #525252; text-decoration: none;">aurora-ai.net</a>
                            </div>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""

    def _text_footer(self) -> str:
        return "\n---\nAurora AI - https://aurora-ai.net\n"

    def send_investigation_started_email(
        self,
        to_email: str,
        incident_data: Dict[str, Any]
    ) -> bool:
        incident_id = incident_data.get('incident_id', 'unknown')
        alert_title = incident_data.get('alert_title', 'Unknown Alert')
        severity = incident_data.get('severity', 'unknown')
        service = incident_data.get('service', 'unknown')
        source_type = incident_data.get('source_type', 'monitoring')
        started_at = incident_data.get('started_at')

        started_str = self._format_timestamp(started_at)
        incident_url = self._get_incident_url(incident_id)
        sev_color = self._get_severity_color(severity)

        subject = f"[Aurora] Investigating: {alert_title}"

        text_body = f"""INVESTIGATION STARTED

Aurora is analyzing an incident from {source_type}

Alert: {alert_title}
Severity: {severity}
Service: {service}
Started: {started_str}

View investigation: {incident_url}{self._text_footer()}"""

        content = f"""
                    <!-- Header Section -->
                    <tr>
                        <td style="padding: 40px 40px 24px 40px;">
                            <table role="presentation" style="width: 100%;">
                                <tr>
                                    <td>
                                        <div style="display: inline-block; background-color: #1c1c1c; border: 1px solid #333333; padding: 4px 12px; margin-bottom: 20px;">
                                            <span style="font-size: 10px; color: #a3a3a3; letter-spacing: 1.5px; text-transform: uppercase;">{source_type}</span>
                                        </div>
                                        <div style="font-size: 11px; font-weight: 600; color: {sev_color['bg']}; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 12px;">
                                            &#9679; Investigation Started
                                        </div>
                                        <div style="font-size: 22px; font-weight: 600; color: #ffffff; line-height: 1.4; margin-bottom: 0;">
                                            {alert_title}
                                        </div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- Divider -->
                    <tr><td style="padding: 0 40px;"><div style="height: 1px; background-color: #262626;"></div></td></tr>

                    <!-- Details -->
                    <tr>
                        <td style="padding: 28px 40px;">
                            <table role="presentation" style="width: 100%; border-collapse: collapse;">
                                <tr>
                                    <td style="width: 50%; padding: 12px 0; vertical-align: top;">
                                        <div style="font-size: 10px; color: #525252; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 6px;">Severity</div>
                                        <div style="display: inline-block; background-color: {sev_color['bg']}; color: {sev_color['text']}; padding: 4px 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px;">{severity}</div>
                                    </td>
                                    <td style="width: 50%; padding: 12px 0; vertical-align: top;">
                                        <div style="font-size: 10px; color: #525252; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 6px;">Service</div>
                                        <div style="font-size: 14px; font-weight: 600; color: #e5e5e5;">{service}</div>
                                    </td>
                                </tr>
                                <tr>
                                    <td colspan="2" style="padding: 12px 0; vertical-align: top;">
                                        <div style="font-size: 10px; color: #525252; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 6px;">Detected</div>
                                        <div style="font-size: 13px; color: #a3a3a3;">{started_str}</div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- Status Indicator -->
                    <tr>
                        <td style="padding: 0 40px 32px 40px;">
                            <div style="background-color: {sev_color['glow']}; border: 1px solid #262626; padding: 16px 20px;">
                                <table role="presentation" style="width: 100%;">
                                    <tr>
                                        <td style="width: 8px; vertical-align: middle;">
                                            <div style="width: 8px; height: 8px; background-color: {sev_color['bg']}; border-radius: 50%;"></div>
                                        </td>
                                        <td style="padding-left: 12px; vertical-align: middle;">
                                            <div style="font-size: 13px; font-weight: 500; color: #e5e5e5;">Aurora is performing root cause analysis...</div>
                                        </td>
                                    </tr>
                                </table>
                            </div>
                        </td>
                    </tr>

                    <!-- CTA -->
                    <tr>
                        <td style="padding: 0 40px 40px 40px;">
                            <table role="presentation" style="width: 100%;">
                                <tr>
                                    <td style="text-align: center;">
                                        <a href="{incident_url}" style="display: inline-block; background-color: #ffffff; color: #000000; padding: 12px 32px; text-decoration: none; font-weight: 600; font-size: 13px; letter-spacing: 0.5px;">
                                            View Investigation &rarr;
                                        </a>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>"""

        html_body = self._base_html(content, accent_color=sev_color['bg'])
        return self._send_email(to_email, subject, html_body, text_body)

    def send_investigation_completed_email(
        self,
        to_email: str,
        incident_data: Dict[str, Any]
    ) -> bool:
        incident_id = incident_data.get('incident_id', 'unknown')
        alert_title = incident_data.get('alert_title', 'Unknown Alert')
        severity = incident_data.get('severity', 'unknown')
        service = incident_data.get('service', 'unknown')
        source_type = incident_data.get('source_type', 'monitoring')
        started_at = incident_data.get('started_at')
        analyzed_at = incident_data.get('analyzed_at')
        aurora_summary = incident_data.get('aurora_summary', 'Analysis complete. View full report for details.')
        status = incident_data.get('status', 'analyzed')

        duration_str = 'Unknown'
        if isinstance(started_at, datetime) and isinstance(analyzed_at, datetime):
            duration = analyzed_at - started_at
            minutes = int(duration.total_seconds() / 60)
            if minutes < 1:
                duration_str = '<1 min'
            elif minutes == 1:
                duration_str = '1 min'
            else:
                duration_str = f'{minutes} min'

        analyzed_str = self._format_timestamp(analyzed_at)
        incident_url = self._get_incident_url(incident_id)
        sev_color = self._get_severity_color(severity)

        subject = f"[Aurora] RCA Complete: {alert_title}"

        max_summary_length = 600
        summary_for_email = aurora_summary
        if len(aurora_summary) > max_summary_length:
            summary_for_email = aurora_summary[:max_summary_length] + '...'

        text_body = f"""RCA COMPLETE

Alert: {alert_title}
Severity: {severity}
Service: {service}
Duration: {duration_str}

ROOT CAUSE:
{summary_for_email}

View full report: {incident_url}{self._text_footer()}"""

        content = f"""
                    <!-- Header Section -->
                    <tr>
                        <td style="padding: 40px 40px 24px 40px;">
                            <table role="presentation" style="width: 100%;">
                                <tr>
                                    <td>
                                        <div style="display: inline-block; background-color: #1c1c1c; border: 1px solid #333333; padding: 4px 12px; margin-bottom: 20px;">
                                            <span style="font-size: 10px; color: #a3a3a3; letter-spacing: 1.5px; text-transform: uppercase;">{source_type}</span>
                                        </div>
                                        <div style="font-size: 11px; font-weight: 600; color: #10b981; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 12px;">
                                            &#10003; Analysis Complete
                                        </div>
                                        <div style="font-size: 22px; font-weight: 600; color: #ffffff; line-height: 1.4;">
                                            {alert_title}
                                        </div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- Divider -->
                    <tr><td style="padding: 0 40px;"><div style="height: 1px; background-color: #262626;"></div></td></tr>

                    <!-- Metrics Row -->
                    <tr>
                        <td style="padding: 28px 40px;">
                            <table role="presentation" style="width: 100%; border-collapse: collapse;">
                                <tr>
                                    <td style="width: 33%; padding: 12px 0; vertical-align: top;">
                                        <div style="font-size: 10px; color: #525252; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 6px;">Severity</div>
                                        <div style="display: inline-block; background-color: {sev_color['bg']}; color: {sev_color['text']}; padding: 4px 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px;">{severity}</div>
                                    </td>
                                    <td style="width: 33%; padding: 12px 0; vertical-align: top;">
                                        <div style="font-size: 10px; color: #525252; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 6px;">Service</div>
                                        <div style="font-size: 14px; font-weight: 600; color: #e5e5e5;">{service}</div>
                                    </td>
                                    <td style="width: 34%; padding: 12px 0; vertical-align: top;">
                                        <div style="font-size: 10px; color: #525252; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 6px;">Resolution Time</div>
                                        <div style="font-size: 14px; font-weight: 600; color: #e5e5e5;">{duration_str}</div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- RCA Summary -->
                    <tr>
                        <td style="padding: 0 40px 32px 40px;">
                            <div style="background-color: #0a0a0a; border: 1px solid #262626; padding: 24px;">
                                <div style="font-size: 10px; font-weight: 600; color: #525252; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 16px;">Root Cause Analysis</div>
                                <div style="font-size: 14px; line-height: 1.7; color: #d4d4d4; white-space: pre-wrap;">{summary_for_email}</div>
                            </div>
                        </td>
                    </tr>

                    <!-- CTA -->
                    <tr>
                        <td style="padding: 0 40px 40px 40px;">
                            <table role="presentation" style="width: 100%;">
                                <tr>
                                    <td style="text-align: center;">
                                        <a href="{incident_url}" style="display: inline-block; background-color: #ffffff; color: #000000; padding: 12px 32px; text-decoration: none; font-weight: 600; font-size: 13px; letter-spacing: 0.5px;">
                                            View Full Report &rarr;
                                        </a>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>"""

        html_body = self._base_html(content, accent_color='#10b981')
        return self._send_email(to_email, subject, html_body, text_body)

    def send_verification_code_email(
        self,
        to_email: str,
        verification_code: str
    ) -> bool:
        subject = "[Aurora] Verify Your Email"

        text_body = f"""VERIFY YOUR EMAIL

Your verification code: {verification_code}

This code expires in 15 minutes.

If you didn't request this, ignore this email.{self._text_footer()}"""

        content = f"""
                    <!-- Header -->
                    <tr>
                        <td style="padding: 48px 40px 24px 40px; text-align: center;">
                            <div style="font-size: 11px; font-weight: 600; color: #525252; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 16px;">
                                Email Verification
                            </div>
                            <div style="font-size: 20px; font-weight: 600; color: #ffffff; line-height: 1.4;">
                                Confirm your notification email
                            </div>
                            <div style="font-size: 13px; color: #737373; margin-top: 12px; line-height: 1.5;">
                                Enter this code in Aurora to start receiving<br>RCA investigation notifications.
                            </div>
                        </td>
                    </tr>

                    <!-- Code Box -->
                    <tr>
                        <td style="padding: 16px 40px 40px 40px;">
                            <div style="background-color: #0a0a0a; border: 1px solid #333333; padding: 32px; text-align: center;">
                                <div style="font-size: 36px; font-weight: 700; letter-spacing: 12px; color: #ffffff; font-family: 'SF Mono', 'Fira Code', 'Courier New', monospace;">
                                    {verification_code}
                                </div>
                                <div style="font-size: 11px; color: #525252; margin-top: 16px; text-transform: uppercase; letter-spacing: 1px;">
                                    Expires in 15 minutes
                                </div>
                            </div>
                        </td>
                    </tr>

                    <!-- Note -->
                    <tr>
                        <td style="padding: 0 40px 40px 40px; text-align: center;">
                            <div style="font-size: 12px; color: #404040;">
                                If you didn't request this, you can safely ignore this email.
                            </div>
                        </td>
                    </tr>"""

        html_body = self._base_html(content, accent_color='#6366f1')
        return self._send_email(to_email, subject, html_body, text_body)

    def send_action_completed_email(
        self,
        to_email: str,
        action_data: Dict[str, Any],
    ) -> bool:
        action_name = action_data.get('action_name', 'Unknown Action')
        status = action_data.get('status', 'success')
        error_msg = action_data.get('error')
        started_at = action_data.get('started_at')
        completed_at = action_data.get('completed_at')
        session_id = action_data.get('session_id')

        duration_str = 'Unknown'
        if isinstance(started_at, datetime) and isinstance(completed_at, datetime):
            duration = completed_at - started_at
            seconds = int(duration.total_seconds())
            if seconds < 60:
                duration_str = f'{seconds}s'
            else:
                duration_str = f'{seconds // 60}m {seconds % 60}s'

        is_success = status == 'success'
        status_label = 'Completed' if is_success else 'Failed'
        accent = '#10b981' if is_success else '#ef4444'
        status_icon = '&#10003;' if is_success else '&#10007;'

        subject = f"[Aurora] Action {status_label}: {action_name}"
        session_url = f"{self.frontend_url}/actions?session={session_id}" if session_id else f"{self.frontend_url}/actions"

        text_body = f"""ACTION {status_label.upper()}

Action: {action_name}
Status: {status_label}
Duration: {duration_str}
"""
        if error_msg:
            text_body += f"Error: {error_msg}\n"
        text_body += f"\nView details: {session_url}{self._text_footer()}"

        error_section = ""
        if error_msg:
            error_section = f"""
                    <!-- Error Details -->
                    <tr>
                        <td style="padding: 0 40px 24px 40px;">
                            <div style="background-color: rgba(239, 68, 68, 0.08); border: 1px solid #3b1111; padding: 20px;">
                                <div style="font-size: 10px; font-weight: 600; color: #ef4444; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 10px;">Error</div>
                                <div style="font-size: 13px; color: #fca5a5; line-height: 1.6; font-family: 'SF Mono', 'Fira Code', monospace;">{error_msg}</div>
                            </div>
                        </td>
                    </tr>"""

        content = f"""
                    <!-- Header Section -->
                    <tr>
                        <td style="padding: 40px 40px 24px 40px;">
                            <table role="presentation" style="width: 100%;">
                                <tr>
                                    <td>
                                        <div style="display: inline-block; background-color: #1c1c1c; border: 1px solid #333333; padding: 4px 12px; margin-bottom: 20px;">
                                            <span style="font-size: 10px; color: #a3a3a3; letter-spacing: 1.5px; text-transform: uppercase;">Automated Action</span>
                                        </div>
                                        <div style="font-size: 11px; font-weight: 600; color: {accent}; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 12px;">
                                            {status_icon} Action {status_label}
                                        </div>
                                        <div style="font-size: 22px; font-weight: 600; color: #ffffff; line-height: 1.4;">
                                            {action_name}
                                        </div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>

                    <!-- Divider -->
                    <tr><td style="padding: 0 40px;"><div style="height: 1px; background-color: #262626;"></div></td></tr>

                    <!-- Details -->
                    <tr>
                        <td style="padding: 28px 40px;">
                            <table role="presentation" style="width: 100%; border-collapse: collapse;">
                                <tr>
                                    <td style="width: 50%; padding: 12px 0; vertical-align: top;">
                                        <div style="font-size: 10px; color: #525252; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 6px;">Status</div>
                                        <div style="display: inline-block; background-color: {accent}; color: #ffffff; padding: 4px 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px;">{status_label}</div>
                                    </td>
                                    <td style="width: 50%; padding: 12px 0; vertical-align: top;">
                                        <div style="font-size: 10px; color: #525252; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 6px;">Duration</div>
                                        <div style="font-size: 14px; font-weight: 600; color: #e5e5e5;">{duration_str}</div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
{error_section}
                    <!-- CTA -->
                    <tr>
                        <td style="padding: 0 40px 40px 40px;">
                            <table role="presentation" style="width: 100%;">
                                <tr>
                                    <td style="text-align: center;">
                                        <a href="{session_url}" style="display: inline-block; background-color: #ffffff; color: #000000; padding: 12px 32px; text-decoration: none; font-weight: 600; font-size: 13px; letter-spacing: 0.5px;">
                                            View Details &rarr;
                                        </a>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>"""

        html_body = self._base_html(content, accent_color=accent)
        return self._send_email(to_email, subject, html_body, text_body)


_email_service = None


def get_email_service() -> EmailService:
    """Get or create the EmailService singleton instance."""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service
