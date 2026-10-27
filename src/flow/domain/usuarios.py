import unicodedata

def normalize(text: str) -> str:
    return (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
        .replace(" ", "_")
    )

def criar_usuario(nome: str, perfil: str) -> dict:
    return {
        "usuario_id": f"user_{normalize(nome)}",
        "nome": nome,
        "perfil": perfil,
        "status": "criado"
    }
