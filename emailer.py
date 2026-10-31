"""Transactional email over SMTP (no-op when SMTP_HOST is unset)."""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import current_app

def _send_email(to_address: str, subject: str, body_text: str, body_html: str = '') -> None:
    """Send a transactional email via SMTP.

    Silently skips when SMTP_HOST is not configured (development mode).
    Raises on SMTP errors in production so callers can handle failures.
    """
    smtp_host = os.environ.get('SMTP_HOST', '')
    if not smtp_host:
        current_app.logger.debug('SMTP_HOST not set — skipping email to %s', to_address)
        return

    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_pw   = os.environ.get('SMTP_PASSWORD', '')
    from_addr = os.environ.get('SMTP_FROM', smtp_user or 'noreply@example.com')

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = from_addr
    msg['To']      = to_address
    msg.attach(MIMEText(body_text, 'plain'))
    if body_html:
        msg.attach(MIMEText(body_html, 'html'))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_pw)
        server.sendmail(from_addr, [to_address], msg.as_string())


def send_claim_invite_email(
    to_email: str,
    child_email: str,
    vault_name: str,
    creator_name: str,
    raw_token: str,
    expires_at,
) -> None:
    """Build and send the child-vault claim invitation email.

    The plaintext token is embedded in the claim URL only — it is never
    returned in API JSON responses (except when EXPOSE_CLAIM_TOKEN=true,
    which is for development and test environments only).
    """
    base_url   = os.environ.get('APP_BASE_URL', 'https://time-vault-imhg.vercel.app').rstrip('/')
    claim_url  = f'{base_url}/?screen=claim&token={raw_token}'
    expiry_str = expires_at.strftime('%B %d, %Y') if hasattr(expires_at, 'strftime') else str(expires_at)

    subject    = f'{creator_name} created a Time Vault for you'
    body_text  = (
        f'Hi!\n\n'
        f'{creator_name} has created a Time Vault called "{vault_name}" for you.\n\n'
        f'Click the link below to claim your vault and access all the memories inside:\n'
        f'{claim_url}\n\n'
        f'This invitation expires on {expiry_str}.\n\n'
        f'— Time Vault'
    )
    import html
    body_html = (
        f'<p>Hi!</p>'
        f'<p><strong>{html.escape(creator_name)}</strong> has created a Time Vault called '
        f'<strong>\"{html.escape(vault_name)}\"</strong> for you.</p>'
        f'<p><a href="{claim_url}">Click here to claim your vault</a></p>'
        f'<p>Invitation expires: {expiry_str}</p>'
    )
    _send_email(to_email, subject, body_text, body_html)
