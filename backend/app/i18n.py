"""Mensagens da API em português e inglês.

O idioma vem do cabeçalho Accept-Language que o frontend envia
(de acordo com a opção escolhida em Configurações).
"""
from fastapi import Request

SUPPORTED = ("pt", "en")
DEFAULT = "pt"

MESSAGES: dict[str, dict[str, str]] = {
    "email_taken": {"pt": "Já existe uma conta com este e-mail.", "en": "An account with this email already exists."},
    "bad_login": {"pt": "E-mail ou senha incorretos.", "en": "Incorrect email or password."},
    "session_expired": {"pt": "Sessão inválida ou expirada. Entre novamente.", "en": "Invalid or expired session. Please sign in again."},
    "excerpt_too_short": {
        "pt": "Cole um trecho um pouco maior (pelo menos {n} letras) para a identificação ser confiável.",
        "en": "Paste a slightly longer excerpt (at least {n} letters) for a reliable match.",
    },
    "empty_query": {"pt": "Digite algo para pesquisar.", "en": "Type something to search."},
    "openlibrary_down": {"pt": "A Open Library não respondeu agora. Tente de novo em alguns instantes.",
                         "en": "Open Library is not responding right now. Try again in a moment."},
    "details_down": {"pt": "Não foi possível carregar os detalhes na Open Library agora.",
                     "en": "Couldn't load details from Open Library right now."},
    "bad_key": {"pt": "Chave de livro inválida.", "en": "Invalid book key."},
    "not_in_catalog": {"pt": "Livro não encontrado no acervo.", "en": "Book not found in the collection."},
    "gutenberg_down": {"pt": "Não foi possível baixar este livro do Project Gutenberg agora.",
                       "en": "Couldn't download this book from Project Gutenberg right now."},
    "catalog_description": {
        "pt": "Obra em domínio público do acervo Freadom, indexada em {n} trechos para identificação por frase.",
        "en": "Public domain work in the Freadom collection, indexed in {n} passages for excerpt matching.",
    },
    "upload_description": {
        "pt": "Livro enviado por você. Fica particular: só você lê e só os seus trechos são comparados com ele ({n} trechos).",
        "en": "Book you uploaded. It's private: only you can read it and only your excerpts are matched against it ({n} passages).",
    },
    "wikisource_description": {
        "pt": "Obra importada da Wikisource (domínio público), indexada em {n} trechos para identificação por frase.",
        "en": "Work imported from Wikisource (public domain), indexed in {n} passages for excerpt matching.",
    },
    "translation_failed": {
        "pt": "A tradução automática não está disponível agora. Mostrando o texto original.",
        "en": "Automatic translation isn't available right now. Showing the original text.",
    },
    "wikisource_down": {"pt": "A Wikisource não respondeu agora. Tente de novo em alguns instantes.",
                        "en": "Wikisource is not responding right now. Try again in a moment."},
    "job_not_found": {"pt": "Importação não encontrada (ela expira depois de uma hora).",
                      "en": "Import not found (it expires after an hour)."},
    "upload_unsupported": {"pt": "Formato não suportado. Envie um arquivo .txt, .epub ou .pdf.",
                           "en": "Unsupported format. Send a .txt, .epub or .pdf file."},
    "upload_too_many": {"pt": "Você chegou ao limite de {n} livros enviados. Apague algum para enviar outro.",
                        "en": "You reached the limit of {n} uploaded books. Delete one to upload another."},
    "upload_too_big": {"pt": "Arquivo grande demais (máximo de {n} MB).", "en": "File too large (max {n} MB)."},
    "upload_empty": {"pt": "Não encontramos texto nesse arquivo.", "en": "We couldn't find any text in this file."},
    "upload_scanned_pdf": {
        "pt": "Esse PDF é feito de imagens (escaneado) e não tem texto para extrair. Tente um PDF com texto ou um EPUB.",
        "en": "This PDF is made of images (scanned) and has no text to extract. Try a text PDF or an EPUB.",
    },
    "upload_bad_file": {"pt": "Não foi possível ler esse arquivo. Ele pode estar corrompido ou protegido.",
                        "en": "Couldn't read this file. It may be corrupted or protected."},
    "upload_save_failed": {"pt": "Não foi possível salvar o livro agora. Tente de novo.",
                           "en": "Couldn't save the book right now. Try again."},
    "too_many_attempts": {"pt": "Muitas tentativas. Aguarde alguns minutos e tente de novo.",
                          "en": "Too many attempts. Wait a few minutes and try again."},
    "reset_sent": {"pt": "Se existir uma conta com esse e-mail, enviamos um link para redefinir a senha.",
                   "en": "If an account with that email exists, we sent a link to reset the password."},
    "bad_token": {"pt": "Este link é inválido, já foi usado ou expirou. Peça um novo.",
                  "en": "This link is invalid, was already used or has expired. Request a new one."},
    "email_verified": {"pt": "E-mail confirmado.", "en": "Email confirmed."},
    "already_verified": {"pt": "Seu e-mail já está confirmado.", "en": "Your email is already confirmed."},
    "verify_sent": {"pt": "Enviamos um novo link de confirmação para o seu e-mail.",
                    "en": "We sent a new confirmation link to your email."},
    "wrong_password": {"pt": "Senha atual incorreta.", "en": "Current password is incorrect."},
    "same_password": {"pt": "A nova senha precisa ser diferente da atual.", "en": "The new password must be different from the current one."},
    "account_deleted": {"pt": "Sua conta e todos os seus dados foram excluídos.", "en": "Your account and all your data were deleted."},
    "not_found": {"pt": "Item não encontrado.", "en": "Item not found."},
    "bad_highlight": {"pt": "O trecho sublinhado é inválido.", "en": "The highlighted passage is invalid."},
}


def get_lang(request: Request) -> str:
    header = (request.headers.get("accept-language") or "").lower()
    for part in header.split(","):
        code = part.split(";")[0].strip()[:2]
        if code in SUPPORTED:
            return code
    return DEFAULT


def msg(key: str, lang: str = DEFAULT, **kwargs) -> str:
    text = MESSAGES[key].get(lang) or MESSAGES[key][DEFAULT]
    return text.format(**kwargs) if kwargs else text
