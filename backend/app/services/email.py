"""Envio de e-mail (confirmação de conta e redefinição de senha).

Provedor escolhido em EMAIL_PROVIDER no .env:
- console: não envia nada; imprime o e-mail no terminal do servidor. Bom para
  desenvolver e para demonstrar o fluxo sem configurar um serviço de e-mail.
- smtp: qualquer servidor SMTP (Gmail com "senha de app", Outlook, Mailtrap,
  ou o SMTP do Amazon SES).
- ses: API do Amazon SES via boto3 (no EC2 usa as credenciais do LabRole).

O envio acontece em segundo plano (BackgroundTasks), então a resposta da API
não espera o servidor de e-mail. `outbox` guarda as últimas mensagens em memória
no modo console, o que permite testar os links nos testes automáticos.
"""
import logging
import smtplib
import ssl
from collections import deque
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr

from ..config import get_settings

log = logging.getLogger("uvicorn.error")


@dataclass
class Message:
    to: str
    subject: str
    text: str
    html: str


outbox: deque[Message] = deque(maxlen=50)


TEMPLATES = {
    "verify": {
        "pt": ("Confirme seu e-mail no Freadom Reading",
               "Olá, {name}!\n\nPara confirmar seu e-mail, abra o link abaixo:\n{link}\n\n"
               "O link vale por {hours} horas. Se você não criou uma conta, ignore esta mensagem."),
        "en": ("Confirm your email on Freadom Reading",
               "Hi {name}!\n\nTo confirm your email, open the link below:\n{link}\n\n"
               "The link is valid for {hours} hours. If you didn't create an account, ignore this message."),
    },
    "reset": {
        "pt": ("Redefinir sua senha do Freadom Reading",
               "Olá, {name}!\n\nRecebemos um pedido para redefinir sua senha. Para escolher uma nova, abra:\n{link}\n\n"
               "O link vale por {minutes} minutos e só pode ser usado uma vez. "
               "Se não foi você, ignore esta mensagem: sua senha continua a mesma."),
        "en": ("Reset your Freadom Reading password",
               "Hi {name}!\n\nWe received a request to reset your password. To choose a new one, open:\n{link}\n\n"
               "The link is valid for {minutes} minutes and can only be used once. "
               "If it wasn't you, ignore this message: your password stays the same."),
    },
}


def _html(text: str, link: str) -> str:
    from html import escape
    body = escape(text).replace(escape(link), f'<a href="{escape(link)}">{escape(link)}</a>').replace("\n", "<br>")
    return (f'<div style="font-family:Georgia,serif;font-size:16px;line-height:1.6;color:#2b1e14;max-width:520px">'
            f'<p style="font-size:20px;font-weight:bold;color:#4a2c17">Freadom Reading</p>{body}</div>')


def build(kind: str, lang: str, to: str, name: str, link: str) -> Message:
    s = get_settings()
    subject, text = TEMPLATES[kind].get(lang, TEMPLATES[kind]["pt"])
    text = text.format(name=name, link=link, hours=s.verify_token_hours, minutes=s.reset_token_minutes)
    return Message(to=to, subject=subject, text=text, html=_html(text, link))


def _send_smtp(msg: Message) -> None:
    s = get_settings()
    em = EmailMessage()
    em["From"], em["To"], em["Subject"] = s.email_from, msg.to, msg.subject
    em.set_content(msg.text)
    em.add_alternative(msg.html, subtype="html")
    # Contexto padrão = confere o certificado do servidor. Sem ele, o starttls() do Python
    # não verifica nada e alguém no meio da rede poderia ler os links de redefinição de senha.
    tls = ssl.create_default_context()
    if s.smtp_port == 465:  # TLS desde o início (SMTPS)
        smtp = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.http_timeout, context=tls)
    else:
        smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.http_timeout)
    with smtp:
        if s.smtp_port != 465 and s.smtp_starttls:
            smtp.starttls(context=tls)
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(em)


def _send_ses(msg: Message) -> None:
    import boto3  # opcional: só com EMAIL_PROVIDER=ses
    s = get_settings()
    boto3.client("ses", region_name=s.aws_region).send_email(
        Source=s.email_from,
        Destination={"ToAddresses": [msg.to]},
        Message={"Subject": {"Data": msg.subject, "Charset": "UTF-8"},
                 "Body": {"Text": {"Data": msg.text, "Charset": "UTF-8"},
                          "Html": {"Data": msg.html, "Charset": "UTF-8"}}},
    )


def send(msg: Message) -> None:
    """Envia a mensagem. Nunca levanta exceção: falha de e-mail não pode derrubar a API."""
    provider = get_settings().email_provider
    try:
        if provider == "smtp":
            _send_smtp(msg)
        elif provider == "ses":
            _send_ses(msg)
        else:
            outbox.append(msg)
            # print (e não log.info): aparece no terminal qualquer que seja o nível de log do uvicorn.
            print("\n===== E-MAIL (EMAIL_PROVIDER=console, não enviado) =====\n"
                  f"Para: {msg.to}\nAssunto: {msg.subject}\n\n{msg.text}\n"
                  "=========================================================", flush=True)
    except Exception:  # noqa: BLE001 — qualquer erro do provedor só é registrado no log
        log.exception("Falha ao enviar e-mail para %s (provedor %s)", parseaddr(msg.to)[1], provider)
