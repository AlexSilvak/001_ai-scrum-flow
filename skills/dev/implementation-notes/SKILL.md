---
name: implementation-notes
description: Como entregar uma user story no workspace: código de verdade + entrega.md com evidências por critério.
---
# Entregável

1. Escreva os **arquivos reais** da solução em `<US-XXX>/` (ex.: `US-001/index.html` com a tela completa, `US-001/app.py`), um `write_workspace_file` por arquivo. Um resumo em texto NÃO é implementação.
2. Por último, escreva `<US-XXX>/entrega.md` com: o que foi feito, os arquivos criados e uma seção **Critérios de aceite atendidos** que repete cada critério literalmente com `[x]` e diz em qual arquivo/trecho ele é atendido.
3. Se houver feedback do QA (retrabalho), corrija os arquivos apontados — não apenas reescreva o resumo.
4. Se a história entrega uma aplicação executável, inclua também o `flow-app.toml` (skill `app-manifest`).
5. Se a solução tem código Python, exponha o app em `<US-XXX>/app.py` (variável `app`, e `PORT`/`HOST` do ambiente em `app.run`). Testes são opcionais para você: se não houver `tests/test_*.py`, o QA escreve `tests/test_qa.py` a partir dos critérios e o sandbox roda o pytest. Se você incluir testes, eles substituem os do QA.
6. Dependências: só o que está em `requirements.txt` (nomes simples, ex.: `flask`). Pacote fora da allowlist do projeto bloqueia a verificação.
