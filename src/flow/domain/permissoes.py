

# services/permissoes.py

# --- 1. Camada de Autorização (ACL) ---
ACL_PERMISSOES = {
    "admin": ["criar_usuario", "conceder_permissao", "revogar_permissao"],
    "suporte_ti": ["conceder_permissao"],
}

USUARIOS_AUTORIZADOS = {
    "alex.silvak23@gmail.com": "admin", # Seu e-mail de teste
}

def verificar_autoridade(user_id: str, intent: str) -> bool:
    """Função que o Orchestrator chama para validar permissão."""
    perfil = USUARIOS_AUTORIZADOS.get(user_id, "comum")
    permissoes = ACL_PERMISSOES.get(perfil, [])
    return intent in permissoes

# --- 2. Camada de Execução ---
def conceder_permissao(nome: str, perfil: str) -> dict:
    return {"usuario": nome, "perfil": perfil, "acao": "concedido", "status": "ok"}

def revogar_permissao(nome: str, perfil: str) -> dict:
    return {"usuario": nome, "perfil": perfil, "acao": "revogado", "status": "ok"}