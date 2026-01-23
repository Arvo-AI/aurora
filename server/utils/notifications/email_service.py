"""Email service for sending RCA investigation notifications via SMTP."""

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
    
    # Severity color scheme (shared across all email types)
    SEVERITY_COLORS = {
        'critical': {'bg': 'linear-gradient(135deg, #dc2626 0%, #991b1b 100%)', 'text': '#ffffff', 'border': '#dc2626'},
        'high': {'bg': 'linear-gradient(135deg, #dc2626 0%, #991b1b 100%)', 'text': '#ffffff', 'border': '#dc2626'},
        'error': {'bg': 'linear-gradient(135deg, #dc2626 0%, #991b1b 100%)', 'text': '#ffffff', 'border': '#dc2626'},
        'warning': {'bg': 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)', 'text': '#ffffff', 'border': '#f59e0b'},
        'medium': {'bg': 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)', 'text': '#ffffff', 'border': '#f59e0b'},
        'info': {'bg': 'linear-gradient(135deg, #6b7280 0%, #4b5563 100%)', 'text': '#ffffff', 'border': '#6b7280'},
        'low': {'bg': 'linear-gradient(135deg, #6b7280 0%, #4b5563 100%)', 'text': '#ffffff', 'border': '#6b7280'},
    }
    DEFAULT_SEVERITY_COLOR = {'bg': 'linear-gradient(135deg, #6b7280 0%, #4b5563 100%)', 'text': '#ffffff', 'border': '#6b7280'}
    
    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.from_email = os.getenv("SMTP_FROM_EMAIL")
        self.from_name = os.getenv("SMTP_FROM_NAME")
        self.frontend_url = os.getenv("FRONTEND_URL")
        
        # Validate required configuration at initialization
        for env_var, attr in [("SMTP_HOST", "smtp_host"), ("SMTP_USER", "smtp_user"), ("SMTP_PASSWORD", "smtp_password")]:
            if not getattr(self, attr):
                raise ValueError(f"EmailService configuration incomplete. Missing required environment variable: {env_var}")
    
    def _send_email(self, to_email: str, subject: str, html_body: str, text_body: str) -> bool:
        """
        Send an email via SMTP.
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            html_body: HTML version of email body
            text_body: Plain text version of email body
            
        Returns:
            True if email sent successfully, False otherwise
        """
        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['From'] = f"{self.from_name} <{self.from_email}>"
            msg['To'] = to_email
            msg['Subject'] = subject
            
            # Attach both plain text and HTML versions
            part1 = MIMEText(text_body, 'plain')
            part2 = MIMEText(html_body, 'html')
            msg.attach(part1)
            msg.attach(part2)
            
            # Connect to SMTP server and send
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
        """Get color scheme for a given severity level."""
        return self.SEVERITY_COLORS.get(severity.lower(), self.DEFAULT_SEVERITY_COLOR)
    
    def _format_timestamp(self, timestamp) -> str:
        """Format timestamp for email display."""
        if isinstance(timestamp, datetime):
            return timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')
        return str(timestamp) if timestamp else 'just now'
    
    def _get_incident_url(self, incident_id: str) -> str:
        """Get the full URL for an incident."""
        return f"{self.frontend_url}/incidents/{incident_id}"
    
    def _get_logo_url(self) -> str:
        """Get the logo URL for email headers."""
        return f"{self.frontend_url}/arvologo.png"
    
    def _text_footer(self) -> str:
        """Generate common text email footer."""
        return "\n---\nAurora AI - Root Cause Analysis Platform\n"
    
    def _severity_badge_html(self, severity: str, sev_color: Dict[str, str]) -> str:
        """Generate HTML for severity badge."""
        return f"""<span style="display: inline-block; background: {sev_color['bg']}; color: {sev_color['text']}; padding: 6px 16px; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px;">
                                                {severity}
                                            </span>"""
    
    def _alert_title_card_html(self, label: str, alert_title: str) -> str:
        """Generate HTML for alert title card."""
        return f"""<div style="background-color: #fafafa; border-left: 4px solid #000000; padding: 24px; margin-bottom: 32px;">
                                <div style="font-size: 11px; font-weight: 600; color: #737373; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 12px;">{label}</div>
                                <div style="font-size: 18px; font-weight: 600; color: #000000; line-height: 1.5;">{alert_title}</div>
                            </div>"""
    
    def _cta_button_html(self, url: str, text: str) -> str:
        """Generate HTML for CTA button."""
        return f"""<table role="presentation" style="width: 100%;">
                                <tr>
                                    <td style="text-align: center; padding: 8px 0;">
                                        <a href="{url}" style="display: inline-block; background-color: #000000; color: #ffffff; padding: 14px 40px; text-decoration: none; font-weight: 600; font-size: 14px; letter-spacing: 0.8px; text-transform: uppercase;">
                                            {text}
                                        </a>
                                    </td>
                                </tr>
                            </table>"""
    
    def _detail_field_html(self, label: str, value: str, is_html: bool = False) -> str:
        """Generate HTML for a detail field."""
        value_html = value if is_html else f'<div style="font-size: 15px; font-weight: 600; color: #000000;">{value}</div>'
        return f"""<div style="font-size: 11px; font-weight: 600; color: #737373; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 12px;">{label}</div>
                                        {value_html}"""
    
    def _email_header_html(self, logo_url: str) -> str:
        """Generate common email header HTML."""
        return f"""<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .animate-fade {{ animation: fadeIn 0.5s ease-out; }}
    </style>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5;">
    <table role="presentation" style="width: 100%; border-collapse: collapse;">
        <tr>
            <td style="padding: 40px 20px;">
                <table role="presentation" class="animate-fade" style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 0; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08); overflow: hidden;">
                    <tr>
                        <td style="background-color: #000000; padding: 40px 32px; text-align: center;">
                            <img src="{logo_url}" alt="Arvo" style="height: 36px; width: auto;" />
                        </td>
                    </tr>"""
    
    def _email_footer_html(self) -> str:
        """Generate common email footer HTML."""
        return """                    <tr>
                        <td style="background-color: #fafafa; padding: 24px 40px; border-top: 1px solid #e5e5e5;">
                            <table role="presentation" style="width: 100%;">
                                <tr>
                                    <td style="text-align: center;">
                                        <div style="font-size: 11px; color: #737373; font-weight: 500; letter-spacing: 0.5px;">
                                            AURORA AI • Root Cause Analysis Platform
                                        </div>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""
    
    def _status_banner_html(self, title: str, subtitle: str) -> str:
        """Generate HTML for status banner section."""
        return f"""                    
                    <!-- Status Banner -->
                    <tr>
                        <td style="background-color: #1a1a1a; padding: 32px; border-bottom: 3px solid #ffffff;">
                            <div style="font-size: 24px; font-weight: 600; color: #ffffff; letter-spacing: -0.3px; text-align: center;">
                                {title}
                            </div>
                            <div style="font-size: 13px; color: #a3a3a3; margin-top: 8px; font-weight: 500; text-align: center; text-transform: uppercase; letter-spacing: 1px;">
                                {subtitle}
                            </div>
                        </td>
                    </tr>"""
    
    def _info_badge_html(self, label: str, value: str) -> str:
        """Generate HTML for an info badge section."""
        return f"""<div style="margin-bottom: 32px;">
                                <div style="font-size: 11px; font-weight: 600; color: #737373; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 8px;">
                                    {label}
                                </div>
                                <div style="font-size: 15px; font-weight: 600; color: #000000;">
                                    {value}
                                </div>
                            </div>"""
    
    def send_investigation_started_email(
        self,
        to_email: str,
        incident_data: Dict[str, Any]
    ) -> bool:
        """
        Send email notification when RCA investigation starts.
        
        Args:
            to_email: Recipient email address
            incident_data: Dictionary containing incident details
                - incident_id: UUID of the incident
                - alert_title: Alert title
                - severity: Alert severity
                - service: Affected service
                - source_type: Monitoring platform (datadog, grafana, netdata)
                - started_at: Investigation start timestamp
                
        Returns:
            True if email sent successfully, False otherwise
        """
        incident_id = incident_data.get('incident_id', 'unknown')
        alert_title = incident_data.get('alert_title', 'Unknown Alert')
        severity = incident_data.get('severity', 'unknown')
        service = incident_data.get('service', 'unknown')
        source_type = incident_data.get('source_type', 'monitoring platform')
        started_at = incident_data.get('started_at')
        
        # Format data
        started_str = self._format_timestamp(started_at)
        incident_url = self._get_incident_url(incident_id)
        logo_url = self._get_logo_url()
        sev_color = self._get_severity_color(severity)
        
        # Subject
        subject = f"[Aurora] RCA Investigation Started - {alert_title}"
        
        # Plain text version
        text_body = f"""INVESTIGATION STARTED

Aurora is analyzing an incident from {source_type}

Alert: {alert_title}
Severity: {severity}
Service: {service}
Started: {started_str}

View investigation: {incident_url}{self._text_footer()}"""
        
        # HTML version
        html_body = f"""{self._email_header_html(logo_url)}
{self._status_banner_html("Investigation Started", "Root Cause Analysis in Progress")}
                    
                    <!-- Content -->
                    <tr>
                        <td style="padding: 48px 40px;">
                            <!-- Source Badge -->
                            {self._info_badge_html("Monitoring Source", source_type)}
                            
                            <!-- Alert Title Card -->
                            {self._alert_title_card_html("Alert Description", alert_title)}
                            
                            <!-- Details Grid -->
                            <table role="presentation" style="width: 100%; border-collapse: collapse; margin-bottom: 40px;">
                                <tr>
                                    <td style="padding: 20px 16px 20px 0; vertical-align: top; width: 50%; border-top: 1px solid #e5e5e5;">
                                        {self._detail_field_html("Severity Level", self._severity_badge_html(severity, sev_color), is_html=True)}
                                    </td>
                                    <td style="padding: 20px 0 20px 16px; vertical-align: top; width: 50%; border-top: 1px solid #e5e5e5;">
                                        {self._detail_field_html("Affected Service", service)}
                                    </td>
        </tr>
        <tr>
                                    <td colspan="2" style="padding: 20px 0 0 0; border-top: 1px solid #e5e5e5;">
                                        <div style="font-size: 11px; font-weight: 600; color: #737373; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 8px;">Timestamp</div>
                                        <div style="font-size: 14px; font-weight: 500; color: #404040;">{started_str}</div>
                                    </td>
        </tr>
                            </table>
                            
                            <!-- CTA Button -->
                            {self._cta_button_html(incident_url, "View Investigation")}
                        </td>
        </tr>
{self._email_footer_html()}"""
        
        return self._send_email(to_email, subject, html_body, text_body)
    
    def send_investigation_completed_email(
        self,
        to_email: str,
        incident_data: Dict[str, Any]
    ) -> bool:
        """
        Send email notification when RCA investigation completes.
        
        Args:
            to_email: Recipient email address
            incident_data: Dictionary containing incident details
                - incident_id: UUID of the incident
                - alert_title: Alert title
                - severity: Alert severity
                - service: Affected service
                - source_type: Monitoring platform
                - started_at: Investigation start timestamp
                - analyzed_at: Investigation completion timestamp
                - aurora_summary: RCA summary text
                - status: Incident status
                
        Returns:
            True if email sent successfully, False otherwise
        """
        incident_id = incident_data.get('incident_id', 'unknown')
        alert_title = incident_data.get('alert_title', 'Unknown Alert')
        severity = incident_data.get('severity', 'unknown')
        service = incident_data.get('service', 'unknown')
        source_type = incident_data.get('source_type', 'monitoring platform')
        started_at = incident_data.get('started_at')
        analyzed_at = incident_data.get('analyzed_at')
        aurora_summary = incident_data.get('aurora_summary', 'Analysis in progress...')
        status = incident_data.get('status', 'analyzed')
        
        # Calculate duration
        duration_str = 'Unknown'
        if isinstance(started_at, datetime) and isinstance(analyzed_at, datetime):
            duration = analyzed_at - started_at
            minutes = int(duration.total_seconds() / 60)
            if minutes < 1:
                duration_str = 'Less than 1 minute'
            elif minutes == 1:
                duration_str = '1 minute'
            else:
                duration_str = f'{minutes} minutes'
        
        # Format data
        analyzed_str = self._format_timestamp(analyzed_at)
        incident_url = self._get_incident_url(incident_id)
        logo_url = self._get_logo_url()
        sev_color = self._get_severity_color(severity)
        
        # Subject
        subject = f"[Aurora] RCA Investigation Complete - {alert_title}"
        
        # Truncate summary for email if too long
        max_summary_length = 500
        summary_for_email = aurora_summary
        if len(aurora_summary) > max_summary_length:
            summary_for_email = aurora_summary[:max_summary_length] + '...\n\n[View full analysis in Aurora]'
        
        # Plain text version
        text_body = f"""ANALYSIS COMPLETE

Aurora has completed the root cause investigation

Alert: {alert_title}
Severity: {severity}
Service: {service}
Duration: {duration_str}
Status: {status}

ROOT CAUSE ANALYSIS:
{summary_for_email}

View full report: {incident_url}{self._text_footer()}"""
        
        # HTML version
        html_body = f"""{self._email_header_html(logo_url)}
{self._status_banner_html("Analysis Complete", f"Investigation Duration: {duration_str}")}
                    
                    <!-- Content -->
                    <tr>
                        <td style="padding: 48px 40px;">
                            
                            <!-- Alert Title Card -->
                            {self._alert_title_card_html("Incident Resolved", alert_title)}
                            
                            <!-- Details Grid -->
                            <table role="presentation" style="width: 100%; border-collapse: collapse; margin-bottom: 40px;">
                                <tr>
                                    <td style="padding: 20px 16px 20px 0; vertical-align: top; width: 33%; border-top: 1px solid #e5e5e5;">
                                        {self._detail_field_html("Severity", self._severity_badge_html(severity, sev_color), is_html=True)}
                                    </td>
                                    <td style="padding: 20px 16px; vertical-align: top; width: 33%; border-top: 1px solid #e5e5e5;">
                                        {self._detail_field_html("Service", service)}
                                    </td>
                                    <td style="padding: 20px 0 20px 16px; vertical-align: top; width: 34%; border-top: 1px solid #e5e5e5;">
                                        {self._detail_field_html("Status", '<span style="display: inline-block; background: linear-gradient(135deg, #000000 0%, #262626 100%); color: #ffffff; padding: 6px 16px; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px;">' + status + '</span>', is_html=True)}
                                    </td>
        </tr>
                            </table>
                            
                            <!-- RCA Summary Section -->
                            <div style="margin-bottom: 40px;">
                                <div style="background-color: #000000; padding: 16px 24px; margin-bottom: 0;">
                                    <div style="font-size: 11px; font-weight: 600; color: #ffffff; text-transform: uppercase; letter-spacing: 1.5px;">
                                        Root Cause Analysis
                                    </div>
                                </div>
                                <div style="background-color: #fafafa; padding: 28px 24px; border: 1px solid #e5e5e5; border-top: none;">
                                    <div style="font-size: 15px; line-height: 1.8; color: #262626; white-space: pre-wrap;">
{summary_for_email}</div>
                                </div>
                            </div>
                            
                            <!-- CTA Button -->
                            {self._cta_button_html(incident_url, "View Full Report")}
                        </td>
        </tr>
{self._email_footer_html()}"""
        
        return self._send_email(to_email, subject, html_body, text_body)
    
    def send_verification_code_email(
        self,
        to_email: str,
        verification_code: str
    ) -> bool:
        """
        Send email verification code for RCA notification recipients.
        
        Args:
            to_email: Email address to verify
            verification_code: 6-digit verification code
            
        Returns:
            True if email sent successfully, False otherwise
        """
        subject = "[Aurora] Verify Your Email for RCA Notifications"
        
        # Plain text version
        text_body = f"""VERIFY YOUR EMAIL

You've requested to receive Aurora RCA investigation notifications at this email address.

Your verification code is: {verification_code}

This code will expire in 15 minutes.

If you didn't request this, you can safely ignore this email.{self._text_footer()}"""
        
        # HTML version with professional styling
        html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5;">
    <table role="presentation" style="width: 100%; border-collapse: collapse;">
        <tr>
            <td style="padding: 40px 20px;">
                <table role="presentation" style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);">
                    <!-- Header -->
                    <tr>
                        <td style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 40px 30px; text-align: center;">
                            <h1 style="margin: 0; color: #ffffff; font-size: 28px; font-weight: 600;">Verify Your Email</h1>
                        </td>
                    </tr>
                    
                    <!-- Content -->
                    <tr>
                        <td style="padding: 40px 30px;">
                            <p style="margin: 0 0 20px 0; color: #374151; font-size: 16px; line-height: 1.6;">
                                You've requested to receive Aurora RCA investigation notifications at this email address.
                            </p>
                            
                            <p style="margin: 0 0 30px 0; color: #374151; font-size: 16px; line-height: 1.6;">
                                Enter this verification code in Aurora:
                            </p>
                            
                            <!-- Verification Code Box -->
                            <div style="background: linear-gradient(135deg, #f3f4f6 0%, #e5e7eb 100%); border-radius: 8px; padding: 30px; text-align: center; margin: 0 0 30px 0;">
                                <div style="font-size: 36px; font-weight: 700; letter-spacing: 8px; color: #1f2937; font-family: 'Courier New', monospace;">
                                    {verification_code}
                                </div>
                            </div>
                            
                            <p style="margin: 0 0 20px 0; color: #6b7280; font-size: 14px; line-height: 1.6;">
                                ⏱️ This code will expire in <strong>15 minutes</strong>.
                            </p>
                            
                            <p style="margin: 0; color: #6b7280; font-size: 14px; line-height: 1.6;">
                                If you didn't request this, you can safely ignore this email.
                            </p>
                        </td>
                    </tr>
                    
                    <!-- Footer -->
                    <tr>
                        <td style="background-color: #f9fafb; padding: 30px; text-align: center; border-top: 1px solid #e5e7eb;">
                            <p style="margin: 0; color: #9ca3af; font-size: 12px;">
                                Aurora AI - Root Cause Analysis Platform
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""
        
        return self._send_email(to_email, subject, html_body, text_body)


# Singleton instance
_email_service = None


def get_email_service() -> EmailService:
    """Get or create the EmailService singleton instance."""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service

