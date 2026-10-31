"""Google Flow automation for the VM worker (Playwright, dedicated persistent Chromium profile).

Flow is driven through its project agent: the prompt goes in the prompt box with the product image
attached, the agent answers with a permission card ("... custa N créditos?"), and only an exact
"Aprovar" click spends credits. Every step before that click is free and safe to abort.
"""
import os
import re
import subprocess
import time
from pathlib import Path

CHROME = Path.home() / ".cache" / "ms-playwright" / "chromium-1234" / "chrome-linux64" / "chrome"
PROMPT_BOX = 'div[role="textbox"][contenteditable="true"]'
COST = re.compile(r"(\d+)\s*cr[eé]ditos?", re.I)
NO_CREDITS = re.compile(r"cr[eé]ditos? insuficientes|n[aã]o (tem|há) cr[eé]ditos|sem cr[eé]ditos|limite de cr[eé]ditos", re.I)
FAILED = re.compile(r"n[aã]o (foi|consegui) (poss[ií]vel )?gerar|falh(a|ou) na gera|viola|pol[ií]tica|n[aã]o posso gerar", re.I)
CHALLENGE = re.compile(r"n[aã]o sou um rob[oô]|unusual traffic|tr[aá]fego incomum|captcha|verify it'?s you|confirme que [eé] voc[eê]", re.I)


class FlowError(Exception):
    """`code` tells the panel what to do; `charged` means credits may already be spent."""

    def __init__(self, code, message, charged=False):
        super().__init__(message)
        self.code, self.charged = code, charged


def ensure_display(display):
    number = display.lstrip(":")
    lock = Path(f"/tmp/.X{number}-lock")
    if lock.exists():
        try:
            os.kill(int(lock.read_text().strip()), 0)
            return None  # A live X server already owns this display.
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # Stale lock from a crashed server: Xvfb below refuses to start if it is really in use.
    process = subprocess.Popen(["Xvfb", display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{number}").exists() and process.poll() is None:
            return process
        time.sleep(0.1)
    raise FlowError("ui_changed", "Xvfb não iniciou.")


class Flow:
    def __init__(self, profile, display=":95", log=print):
        self.profile, self.display, self.log = str(profile), display, log
        self.playwright = self.context = self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # Only the VM worker venv has Playwright.
        ensure_display(self.display)
        self.playwright = sync_playwright().start()
        try:
            # --no-sandbox only for this dedicated profile: approved by the user (Ubuntu blocks userns sandboxing).
            self.context = self.playwright.chromium.launch_persistent_context(
                self.profile, executable_path=str(CHROME) if CHROME.exists() else None, headless=False,
                args=["--no-sandbox", "--window-size=1440,900", "--no-first-run", "--no-default-browser-check"],
                viewport={"width": 1440, "height": 900}, accept_downloads=True, locale="pt-BR",
                env={**os.environ, "DISPLAY": self.display})
        except Exception as error:
            self.playwright.stop()
            busy = "ProcessSingleton" in str(error) or "SingletonLock" in str(error) or "in use" in str(error)
            raise FlowError("browser_busy" if busy else "ui_changed",
                            "Perfil do Flow em uso por outro navegador (login em andamento?)." if busy
                            else f"Chromium não abriu: {str(error)[:200]}") from None
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(30000)
        return self

    def __exit__(self, *exc):
        try:
            if self.context:
                self.context.close()
        finally:
            if self.playwright:
                self.playwright.stop()

    # ---------- helpers ----------
    def screenshot(self, path):
        try:
            self.page.screenshot(path=str(path))
        except Exception:
            pass

    def text(self):
        return self.page.evaluate("document.body ? document.body.innerText : ''")

    def click_label(self, label, last=True, timeout=10):
        """Click a button by aria-label through the DOM (hover-only controls report as not visible)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            done = self.page.evaluate("""([label, last]) => {
                const all = [...document.querySelectorAll('button')].filter(b => b.getAttribute('aria-label') === label && !b.hidden);
                const visible = all.filter(b => b.offsetParent !== null);
                const target = (visible.length ? visible : all)[last ? (visible.length ? visible : all).length - 1 : 0];
                if (!target) return false; target.click(); return true; }""", [label, last])
            if done:
                return
            time.sleep(0.5)
        raise FlowError("ui_changed", f"Botão '{label}' não encontrado no Flow.")

    def check_session(self):
        url = self.page.url
        if "accounts.google.com" in url or "/signin" in url:
            raise FlowError("login_required", "Sessão do Google expirada: faça login no navegador da VM.")
        if CHALLENGE.search(self.text()):
            raise FlowError("challenge", "O Google pediu verificação humana (CAPTCHA ou confirmação de identidade).")

    # ---------- steps ----------
    def open_project(self, url):
        if not re.match(r"^https://(flow\.google\.com|labs\.google)/", url or ""):
            raise FlowError("no_project", "Configure a URL do projeto do Flow nas Preferências.")
        self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        deadline = time.time() + 90
        while time.time() < deadline:
            self.check_session()
            if self.page.locator(PROMPT_BOX).count():
                return
            if self.page.locator('a[href*="accounts.google.com/ServiceLogin"], a[href*="signin"]').count():
                raise FlowError("login_required", "Flow aberto sem login: faça login no navegador da VM.")
            time.sleep(1)
        raise FlowError("ui_changed", "Caixa de comando do Flow não apareceu.")

    def ensure_settings(self, model):
        """Agent settings: ask before generating (so the cost can be checked), 9:16, x1, expected model."""
        self.page.evaluate("""() => { const t = [...document.querySelectorAll('button[aria-label="Configurações"]')]
            .filter(b => b.innerText.trim() === 'tune').pop(); if (t) t.click(); }""")
        self.page.wait_for_selector("flow-settings-view", timeout=15000)
        state = self.page.evaluate("""(model) => {
            const view = document.querySelector('flow-settings-view');
            const radios = [...view.querySelectorAll('input.mdc-radio__native-control')];
            const always = radios.find(r => (r.closest('mat-radio-button')?.innerText || '').trim().startsWith('Sempre'));
            const changes = [];
            if (!always) return {error: 'opção "Confirmar antes de gerar" não encontrada'};
            if (!always.checked) { always.click(); changes.push('confirmar antes de gerar'); }
            const head = [...view.querySelectorAll('*')].find(e => e.childElementCount === 0 && e.textContent.trim() === 'Padrão para geração de vídeo');
            const section = head && head.closest('.settings-section');
            if (!section) return {error: 'seção de vídeo não encontrada'};
            for (const label of ['9:16', 'x1']) {
                const toggle = [...section.querySelectorAll('[role=radio]')].find(b => b.innerText.trim().endsWith(label));
                if (!toggle) return {error: 'opção ' + label + ' não encontrada'};
                if (toggle.getAttribute('aria-checked') !== 'true') { toggle.click(); changes.push(label); }
            }
            const current = (section.querySelector('button[aria-label="Modelo padrão de geração de vídeo"]')?.innerText || '').replace('arrow_drop_down', '').trim();
            return {changes, model: current};
        }""", model)
        if state.get("error"):
            raise FlowError("ui_changed", "Configurações do agente: " + state["error"])
        if model.casefold() not in state["model"].casefold():
            self.click_label("Voltar")
            raise FlowError("model_mismatch", f"Modelo padrão de vídeo no Flow é '{state['model']}', esperado '{model}'.")
        if state["changes"]:
            self.page.evaluate("() => document.querySelector('flow-settings-view button.settings-save-button')?.click()")
            self.log("Flow: ajustado " + ", ".join(state["changes"]))
            time.sleep(2)
        if self.page.locator("flow-settings-view").count():  # Saving closes the view by itself.
            try:
                self.click_label("Voltar", timeout=3)
            except FlowError:
                pass
        time.sleep(1)

    def new_session(self):
        try:
            self.click_label("Iniciar nova sessão", timeout=5)
            time.sleep(2)
        except FlowError:
            self.log("Flow: botão de nova sessão ausente; seguindo na sessão atual.")
        self.page.wait_for_selector(PROMPT_BOX, timeout=20000)

    def attach_image(self, image):
        name = Path(image).name
        self.click_label("Adicionar elementos à caixa de comando", last=False)
        upload = self.page.locator(".cdk-overlay-container button", has_text="Enviar mídia").first
        upload.wait_for(timeout=15000)
        with self.page.expect_file_chooser(timeout=15000) as chooser:
            upload.click()
        chooser.value.set_files(str(image))
        item = self.page.locator(".cdk-overlay-container button", has_text=name).first
        item.wait_for(timeout=90000)
        for _ in range(60):  # The item exists while still uploading; wait until it is clickable.
            if self.page.evaluate("""(name) => { const b = [...document.querySelectorAll('.cdk-overlay-container button')]
                .find(b => b.innerText.includes(name)); return !!b && !b.disabled && !b.querySelector('mat-progress-spinner, [role=progressbar]'); }""", name):
                break
            time.sleep(1)
        item.click()
        include = self.page.locator(".cdk-overlay-container button", has_text="Incluir no comando").first
        include.wait_for(timeout=15000)
        include.click()
        for _ in range(30):
            if self.page.evaluate("document.querySelectorAll('flow-creative-agent-prompt-box img, flow-base-prompt-box img').length"):
                return
            time.sleep(1)
        raise FlowError("ui_changed", "A imagem não entrou na caixa de comando.")

    def tiles(self):
        return self.page.evaluate("document.querySelectorAll('flow-video-tile').length")

    def submit(self, prompt):
        box = self.page.locator(PROMPT_BOX).first
        box.click()
        self.page.keyboard.insert_text(prompt)
        time.sleep(2)
        pending = lambda: self.page.evaluate(f"(document.querySelector('{PROMPT_BOX}')?.innerText || '').trim().length")
        for attempt in range(3):
            if attempt == 0:
                self.click_label("Iniciar geração")
            else:  # The send button can stay inert right after an upload; Enter submits the box.
                self.page.locator(PROMPT_BOX).first.click()
                self.page.keyboard.press("End")
                self.page.keyboard.press("Enter")
            for _ in range(10):
                time.sleep(1)
                if not pending():
                    return
        raise FlowError("ui_changed", "O Flow não aceitou o envio do comando.")

    def wait_permission(self, timeout=240):
        """Return (cost, text) of the agent's permission card; fail on questions or credit problems."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_session()
            card = self.page.evaluate("""() => {
                const cards = [...document.querySelectorAll('flow-permission-message')];
                const open = cards.filter(c => c.querySelector('[role=radio][aria-label="Aprovar"][aria-disabled="false"]')
                    && !c.querySelector('[role=radio][aria-checked="true"]'));
                const last = open.pop();
                const question = document.querySelector('flow-a2ui-multiple-choice, flow-a2ui-video-option');
                return {text: last ? last.innerText : '', question: question ? question.innerText.slice(0, 300) : ''};
            }""")
            if card["text"]:
                match = COST.search(card["text"])
                if not match:
                    raise FlowError("ui_changed", "Cartão de confirmação sem custo legível: " + card["text"][:150])
                return int(match.group(1)), card["text"].strip()
            agent = self.agent_text()
            if NO_CREDITS.search(agent):
                raise FlowError("no_credits", "Flow sem créditos suficientes: " + agent[-200:])
            if card["question"]:
                raise FlowError("agent_question", "O agente do Flow fez uma pergunta em vez de pedir confirmação: " + card["question"])
            time.sleep(2)
        raise FlowError("ui_changed", "O agente do Flow não pediu confirmação no prazo.")

    def agent_text(self):
        return self.page.evaluate("[...document.querySelectorAll('flow-chat-bubble')].slice(-3).map(b => b.innerText).join('\\n')")

    def answer(self, approve):
        """Pick exactly "Aprovar" or "Rejeitar" (never "Sempre aprovar": it would disable the cost check)."""
        label = "Aprovar" if approve else "Rejeitar"
        option = self.page.locator(f'flow-permission-message [role="radio"][aria-label="{label}"][aria-disabled="false"]').last
        if not option.count():
            raise FlowError("ui_changed", f"Opção {label} não encontrada no cartão de confirmação.")
        option.click()
        for _ in range(20):
            time.sleep(0.5)
            if not self.page.locator(f'flow-permission-message [role="radio"][aria-label="{label}"][aria-disabled="false"][aria-checked="false"]').count():
                return
        raise FlowError("ui_changed", f"O Flow não registrou a opção {label}.", charged=approve)

    def wait_render(self, before, timeout=900):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_session()
            state = self.page.evaluate("""(before) => {
                const tiles = [...document.querySelectorAll('flow-video-tile')];
                const first = tiles[0];
                const ready = !!first && !!first.querySelector('img.thumbnail[src], video[src]') && !first.querySelector('mat-progress-spinner, [role=progressbar]');
                return {count: tiles.length, ready, label: first ? first.innerText.trim().slice(0, 80) : ''};
            }""", before)
            if state["count"] > before and state["ready"]:
                return
            agent = self.agent_text()
            if FAILED.search(agent) and state["count"] <= before:
                raise FlowError("render_failed", "Flow informou falha na geração: " + agent[-250:], charged=True)
            time.sleep(5)
        raise FlowError("render_timeout", "O vídeo não ficou pronto no prazo; confira o projeto no Flow.", charged=True)

    def download(self, destination):
        self.page.evaluate("""() => { const tile = document.querySelector('flow-video-tile');
            const more = tile && [...tile.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Mais opções');
            if (more) more.click(); }""")
        item = self.page.locator('.cdk-overlay-container [role="menuitem"]', has_text=re.compile("download", re.I)).first
        item.wait_for(timeout=15000)
        item.click()
        size = self.page.locator('.cdk-overlay-container [role="menuitem"]', has_text=re.compile("720p|Tamanho original", re.I)).first
        size.wait_for(timeout=15000)
        with self.page.expect_download(timeout=180000) as pending:
            size.click()
        pending.value.save_as(str(destination))
        return Path(destination)

    def generate(self, project_url, image, prompt, expected_cost, model, before_approve, on_stage,
                 workdir, dry_run=False):
        self.open_project(project_url)
        on_stage("preparing")
        self.ensure_settings(model)
        self.new_session()
        self.attach_image(image)
        before = self.tiles()
        self.submit(f"Gere agora exatamente 1 vídeo com o modelo {model}, proporção 9:16, 10 segundos, usando a "
                    f"imagem anexada como referência do produto. Não faça perguntas. {prompt}")
        cost, text = self.wait_permission()
        self.log(f"Flow pediu confirmação: {text[:160]}")
        if cost != expected_cost or not re.search(r"\b1 gera[cç][aã]o\b", text):
            self.answer(False)
            raise FlowError("cost_mismatch", f"Flow pediu {cost} créditos ('{text[:120]}'); o esperado era {expected_cost} "
                                             "para 1 geração. Geração recusada.")
        if dry_run:
            self.answer(False)
            return None
        try:
            before_approve(cost)  # The panel records "credits may be spent" before the click.
        except Exception:
            self.answer(False)
            raise
        self.answer(True)
        on_stage("submitted", credits=cost)
        self.wait_render(before)
        on_stage("downloading")
        return self.download(Path(workdir) / "flow.mp4")
