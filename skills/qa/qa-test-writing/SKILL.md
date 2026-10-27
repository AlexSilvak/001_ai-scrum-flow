---
name: qa-test-writing
description: Como o QA escreve tests/test_qa.py (pytest e Playwright) a partir dos critérios de aceite.
---
# Testes do QA

Você só escreve `<US-XXX>/tests/test_qa.py` (um único arquivo) com `write_workspace_file`. Leia antes o código entregue (`arquivos_entregues`) com `read_workspace_file`.

1. **Um teste por critério de aceite**, com nome que cite o critério (`test_login_invalido_mostra_erro`). Assert em comportamento, nunca `assert True`.
2. **Importe o app assim** (o teste roda de dentro de `tests/`):
   ```python
   import os, sys
   sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
   from app import app
   ```
3. **Flask sem navegador:** `client = app.test_client()`; `client.post("/login", data={...})`; confira status, redirect e texto da mensagem de erro; rota protegida sem sessão deve redirecionar/401.
4. **Tela/navegador (Playwright)** — só quando o critério é visual ou de interação. O sandbox já tem Chromium e o fixture `start_app`:
   ```python
   from playwright.sync_api import Page, expect

   def test_tela_de_login(start_app, page: Page):
       url = start_app("app.py")            # sobe `python app.py` com PORT/HOST e espera abrir
       page.goto(url + "/login")
       page.fill("input[name=email]", "a@b.com")
       page.fill("input[name=senha]", "errada")
       page.click("button[type=submit]")
       expect(page.locator("body")).to_contain_text("inválid")
   ```
   Use `page`/`browser` do pytest-playwright (não chame `sync_playwright()` à mão). Sem internet: nada de CDN no teste.
5. Banco de dados: use SQLite em memória ou um arquivo em `/tmp`; nunca `localhost:5432`.
6. Não escreva em outro caminho: qualquer outro arquivo é recusado.
