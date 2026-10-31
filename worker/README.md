# Worker da VM hermes

O painel roda no Unraid em container somente leitura. O Antigravity CLI (`agy`) logado na sua conta fica na VM `hermes`. O worker liga os dois: busca tarefas no painel, executa o `agy` e devolve o resultado.

## Como funciona

- O worker chama `POST /api/worker/claim` a cada 5 segundos com o token do worker. O painel nunca abre conexão para a VM.
- Cada análise de produto vira uma tarefa `llm`. O worker baixa as imagens do anúncio e dos candidatos para uma pasta temporária, roda `agy -p … --model <modelo> --add-dir <pasta> --output-format json --json-schema …` e devolve a saída estruturada. Ao terminar, apaga a pasta.
- O `agy` roda **sem** `--dangerously-skip-permissions`. Títulos de anúncios são texto de terceiros. Sem essa opção, o modo headless nega ferramentas de terminal: uma instrução maliciosa no título não executa comandos na VM. A leitura das imagens continua funcionando, e isso foi testado na VM.
- Sem contato por 90 segundos, o worker fica offline. O painel pula os modelos `agy` e segue para os fallbacks OpenAI-compatíveis configurados.
- Tarefas têm prazo. Se uma tarefa vencer antes de o worker responder, o resultado atrasado é recusado. O painel não espera tarefas criadas antes de reiniciar.

## Instalação (uma vez)

1. No painel: Preferências → Worker da VM → **Gerar novo token**. Copie o token.
2. Na VM `hermes`, dentro do checkout do repositório:

       worker/install.sh http://192.168.1.90:8787

   Cole o token quando o script pedir. Ele fica em `~/.config/shopee-studio-worker/config.json` com modo 600.
3. Para o serviço continuar após o logout e voltar após um reboot, o usuário precisa de linger: `loginctl enable-linger hermes`.

Atualizar o worker é rodar `worker/install.sh` de novo. O script preserva o config e reinicia o serviço.

## Operação

    systemctl --user status shopee-studio-worker
    journalctl --user -u shopee-studio-worker -f
    systemctl --user stop shopee-studio-worker     # o painel passa a usar os fallbacks

Trocar o token no painel desconecta o worker. Depois, edite `config.json` com o token novo e reinicie o serviço.
