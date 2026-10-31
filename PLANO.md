# Plano — Shopee Studio: anúncios equivalentes, conteúdo e vídeo

Atualizado em 2026-10-31. Status: implementação autorizada ("Implemente o plano"), em dois PRs. PR 1 (conteúdo, links equivalentes, LLM com fallbacks, Telegram novo) implementado na branch `feat/conteudo-links-llm`; PR 2 (worker do Flow, legenda e envio do vídeo) pendente. Veja **Andamento** no fim.

## Objetivo e decisões

- Manter seleção dos cinco produtos principais e regras existentes de filtros/histórico.
- Buscar anúncios do mesmo produto para vincular ao mesmo vídeo: entregar até **6 links no total por produto** (o anúncio selecionado + até 5 equivalentes idênticos validados) em uma lista única, sem fazer distinção visual entre "principal" e "equivalente".
- Não confundir semelhante com idêntico. Comparar imagens E atributos (marca/modelo, variante/cor, tamanho/capacidade, acessórios e quantidade do kit). Fotos iguais podem ser reutilizadas em anúncios diferentes.
- Classificar candidatos como correspondência sustentada por evidências, incerto ou incompatível; explicar motivos. Não preencher vagas com itens apenas parecidos. Elegibilidade e comissão do canal Vídeo precisam de validação separada.
- Remover título de vídeo separado; usuário não usa. Manter nome do produto apenas como identificação.
- Telegram: por produto, uma mensagem com foto (imagem original do anúncio baixada pelo painel e enviada como foto), nome, preço/comissão, legenda num bloco `<pre>` copiável e os links numerados. O prompt chega num segundo bloco `<pre>`, como resposta à mensagem da foto. Sem botões de cópia. Se a legenda da foto passar de 1.024 caracteres, os links vão para a resposta. Se a foto for recusada, a mensagem sai só em texto, com aviso no histórico. Falta conferir a cópia dos blocos no cliente Telegram do usuário.
- A imagem é a original do anúncio (`imageUrl` da API, ~1000 px). Nunca uma imagem gerada por IA. O print do anúncio para conferência não foi implementado, porque exigiria um navegador no painel. Ele pode entrar no PR 2, pelo worker.
- Criar prompt de venda específico ao produto: gancho, demonstração, benefício e CTA dentro da plataforma. Preservar aparência do produto, sem inventar atributos, descontos, escassez, depoimentos ou acessórios.
- **Legenda da dublagem (subtitles):** gerar legenda sincronizada para a fala/narração do vídeo (para converter quem assiste sem áudio ligado). O Google Flow gera o vídeo limpo (sem textos renderizados pela IA de vídeo para evitar erros de grafia em português) e o pós-processamento local na VM (`ffmpeg` + alinhamento/transcrição do áudio ou roteiro da fala) aplica a legenda estilizada no `.mp4` antes do envio ao Telegram.
- Duração corrigida pelo usuário para 10 segundos. Modelo usado e aprovado no teste: Omni 1.1 Flash. Vídeo vertical 9:16; teste em 720p e uma versão.
- **LLM de visão/texto (padrão + fallbacks configuráveis):**
  - **Padrão de fábrica (Antigravity CLI `agy`):** principal `gemini-3.8-flash-medium`, fallback `gemini-4-argon-medium`. O worker da VM executa o agy e lê as imagens numa pasta temporária com `--add-dir <pasta>`, `--output-format json` e `--json-schema`. Ele roda **sem** `--dangerously-skip-permissions`. Teste de 2026-10-31: com essa opção, o agy executou um comando fora da pasta mesmo com `--sandbox`. Sem ela, o modo headless nega o terminal, e a leitura de imagem continua funcionando. Os títulos dos anúncios são texto de terceiros, então rodar sem essa opção evita injeção de comandos.
  - **Alteração de modelo e Fallbacks na UI:** permitir na página de configuração alterar o modelo principal e configurar uma cadeia de **fallbacks** (outros modelos do `agy`, como `gemini-4-argon` / `claude-sonnet-5-5`, ou endpoints OpenAI-compatíveis / OpenRouter com endpoint, API key protegida, modelo visual e modelo de texto opcional), além de botão de teste real de visão, orçamento de candidatos, timeout e retries. Não presumir que a conexão Antigravity de chat gera vídeos.

## Descrição e hashtags

Antes: `social_copy` em `core.py` criava título, descrição genérica e hashtags fixas, e cortava em 256 caracteres. Foi substituída por `content.py` + `analysis.py` (PR 1).

Regras implementadas:
1. Extrair benefício verificável e contexto de uso, sem repetir nome longo do anúncio.
2. Escolher hashtag específica do produto e hashtag do uso/contexto; terceira apenas quando útil e couber. Isso é decisão editorial, não regra da plataforma.
3. Reservar espaço das tags/separadores antes de finalizar a frase.
4. Contar descrição + hashtags + espaços/pontuação por código; conferir Unicode com o aplicativo. Evitar emojis por padrão.
5. Reescrever excessos, não truncar frases/tags ou acrescentar reticências. Exibir contagem fora do bloco copiável.
6. Não usar #Shopee/#Achadinhos/#Viral/#Fyp automaticamente, nem palavras soltas do anúncio.

Limite definido pelo usuário: **150 caracteres** no conjunto completo (descrição + hashtags).

Fontes consultadas:
- https://plugg.to/shopee-video-pluggto — legenda até 150 caracteres no painel da integração.
- https://shopee.com.br/blog/shopee-video-o-que-e-como-usar — recomenda legenda atrativa e hashtags relevantes, sem teto explícito na página consultada.

## Dependências de dados

`productOfferV2` aceita keyword e `sortType: 1` (relevância). Desde o PR 1, a consulta coleta `imageUrl` e `productLink`. Validado em 2026-10-31 com a API real: imagens em `cf.shopee.com.br`, ~1000 px. A API não traz especificações nem variantes, então a comparação usa uma imagem + título por anúncio. A busca real por equivalentes trouxe de 1 a 50 resultados por produto.

## Teste concluído — Flow pelo Hermes Desktop

- Navegador inicial de browser_exec era sessão separada, não visível ao usuário. Login concluído depois no painel desktop_preview; controle via drive_preview.
- Criado projeto separado: https://flow.google.com/project/de481422-1c45-4983-b85b-67540319a3f5
- Nome mostrado: `out. 30 - 18:40`.
- Referência escolhida do projeto existente `Anuncios`: `White wireless earbuds in case`. A escolha não alterou o projeto de origem.
- Configuração: Omni 1.1 Flash, 10s, vertical 9:16, 720p, uma versão.
- Custo exibido: 15 créditos (8s mostrava 12). Usuário confirmou explicitamente 15 créditos.
- Uma geração foi enviada; acompanhado progresso até aparecer cartão reproduzível `Creating vertical product advert…`.
- Usuário assistiu e afirmou: “funcionou bem”.
- Download não foi validado. Agente não fez revisão visual/áudio própria. Não afirmar que pipeline completo ou operação autônoma já foi provado.
- Plugin instalado antigravity-subscription-directsdk opera chat_completions via agy, não expõe geração Flow direta.

## Arquitetura proposta para execução sem PC

Processo/worker na VM com navegador e perfil persistente próprios, independente do Hermes Desktop. Agendamento → seleção → evidências/validação → copy/prompt → upload no Flow → geração → download → legendagem da dublagem (`ffmpeg`) → Telegram.

Requisitos operacionais:
- Fila persistente e estado por geração.
- Não reenviar geração após timeout sem reconciliar a anterior, para evitar crédito duplicado.
- Orçamento/limite de créditos e confirmação de custo inesperado.
- Pausar e alertar em login expirado/2FA/CAPTCHA; intervenção humana, nunca contornar desafio.
- Não copiar sessão do PC por suposição. Autenticar o navegador da VM diretamente.
- Testar perfil após reinício, upload de arquivo da VM, geração/download e execução com Desktop fechado.
- Tela virtual na VM é opção para navegador com janela; PC serve somente de visualizador para login/manutenção.

## TESTE CONCLUÍDO — navegador independente na VM (`hermes`)

Host confirmado: `hermes`, Linux. Chromium Playwright em `/home/hermes/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`; Playwright, Xvfb e `ffprobe`/`ffmpeg` disponíveis.

Ambiente isolado de teste em `/home/hermes/.local/share/flow-vm-test` (modo 700, sem alterar código/configuração do Shopee Studio):
- `runtime`: pacotes `x11vnc`, `libvncserver1`, `libvncclient1` e `novnc` extraídos em espaço de usuário.
- `web-venv`: `websockify` para proxy noVNC.
- `profile`: perfil persistente dedicado do Chromium na VM.
- `downloads`: diretório de saída validado para arquivos baixados via CDP.

### Diagnóstico de `sudo` / sandbox e decisão do usuário
- Verificado com `passwd -S hermes` (`hermes L 2026-08-21 0 99999 7 -1`) que a conta `hermes` na VM foi criada apenas com chave SSH (senha bloqueada) e sem regra `NOPASSWD` em `/etc/sudoers.d/`.
- Usuário autorizou iniciar o Chromium dedicado na VM com `--no-sandbox` restrito ao perfil exclusivo `/home/hermes/.local/share/flow-vm-test/profile`.

### Etapas validadas em 2026-10-30
1. **Login via túnel reverso seguro (`OK`):** `Xvfb :95` + `x11vnc` (`127.0.0.1:5905`) + `noVNC` (`127.0.0.1:6085`) + túnel reverso SSH para `pc1-lan`. Usuário concluiu login na conta Google (`PRO`) diretamente no navegador da VM.
2. **Persistência de sessão após reinício (`OK`):** Chromium encerrado com `SIGTERM` e reiniciado em novo processo apontando para `/home/hermes/.local/share/flow-vm-test/profile`; reabriu autenticado diretamente nos projetos do Google Flow.
3. **Upload real de arquivo a partir do disco da VM (`OK`):** Imagem `/home/hermes/.local/share/flow-vm-test/downloads/white_earbuds_vm_upload_test.png` (35 KB) enviada via `expect_file_chooser` (`Adicionar elementos à caixa de comando` → `Enviar mídia` → `Incluir no comando`), gerando novo chip de referência no prompt.
4. **Nova geração completa na VM (`OK`):** Configurado e confirmado pelo usuário o consumo de **15 créditos** (`Omni 1.1 Flash`, `10s`, vertical `9:16`, `720p`, `1 versão`). Fluxo de confirmação do agente do Flow (`Aprovar`) acionado via CDP e geração concluída em ~45s (`Creating vertical product video …`).
5. **Download real via CDP (`OK`):** Configurado `Browser.setDownloadBehavior` para `/home/hermes/.local/share/flow-vm-test/downloads` e acionado `Mais opções` → `Fazer o download` → `720p Tamanho original`. Ambos os vídeos (o do teste anterior `Creating_vertical_product_advert…_ca4023e3.mp4` de 2,9 MB e o da nova geração na VM `Creating_vertical_product_video_…_f320fa55.mp4` de 2,8 MB) foram salvos e verificados no disco da VM.
6. **Encerramento limpo (`OK`):** Processos de teste (`Chromium`, `x11vnc`, `websockify`, `Xvfb` e túnel SSH) encerrados após o teste, preservando `/home/hermes/.local/share/flow-vm-test/profile` e `downloads` prontos para automação futura.

## Andamento

### PR 1 — conteúdo, links equivalentes, LLM, Telegram (branch `feat/conteudo-links-llm`)

| # | Pedido | Como ficou | Estado |
|---|---|---|---|
| 1 | Até 6 links por produto, sem distinguir principal | `analysis.py`: busca por relevância com as 6 primeiras palavras do nome (1 página, 50 itens). Pré-filtro: preço entre 0,5x e 2x, similaridade de título ≥ 0,25, com imagem. O modelo classifica até N candidatos (padrão 8) como match, uncertain ou incompatible, com motivo. Só match vira link, até 5 + o selecionado | feito, testado com dados reais |
| 2 | Não confundir semelhante com idêntico | Prompt exige imagem E atributos e diz que foto igual não prova nada. Candidato não avaliado = incerto. Sem imagem do principal, nenhum match. Teste real: bicicleta 24V × 21V e guarda-roupas de outra cor ou modelo saíram como incompatíveis; omeleteira idêntica de 8 lojas, como match | feito |
| 3 | Remover título | removido (`social_copy`, botões e título) | feito |
| 4 | Legenda + hashtags ≤ 150, sem truncar | `content.copy_problems` valida tudo (UTF-16, 2 ou 3 tags, proibidas, emoji, reticências, preço/desconto/frete/urgência). Resposta inválida volta ao modelo com os problemas listados | feito |
| 5 | Prompt específico, 10 s | O modelo devolve gancho, demonstração, benefício, chamada e narração (≤ 25 palavras). `content.video_prompt` monta o prompt com regras fixas: preservar o produto, sem texto na tela, sem preço | feito |
| 6 | LLM configurável + fallbacks + teste | Preferências → Modelo de visão e texto: até 6 modelos (agy ou OpenAI-compatível), ordem, ativo, chave protegida, modelo de texto, timeout, tentativas, orçamento de candidatos. Testar visão manda um PNG vermelho e exige a cor certa | feito, agy validado na VM |
| 7 | Telegram novo | Mensagem com foto por produto + prompt em resposta. Entrega por produto (`deliveries`): incerto só exclui o produto afetado. Recusa definitiva sem nada enviado volta para "não enviado" | feito, falta teste no Telegram real |
| 8 | Execução sem PC | Worker na VM (`worker/`) por polling com token, tarefas com prazo, heartbeat. Offline = agy pulado na hora | feito. Serviço systemd só depois do deploy |

Decisões tomadas sem perguntar:
- Fallback padrão `gemini-4-argon-medium` (agy), para a cadeia já vir com dois modelos sem custo extra.
- Equivalentes enviados também ficam excluídos das próximas consultas (evita outro vídeo do mesmo produto).
- Se todos os modelos falharem: o produto sai com o próprio link e um texto automático, marcado como "sem validação do modelo" no painel e no Telegram. Ele não bloqueia o envio diário.
- O painel baixa a imagem e envia por upload. Se o download falhar, o Telegram busca a URL. Se a foto for recusada, a mensagem sai só em texto.
- Erro 4xx de endpoint OpenAI-compatível (chave/modelo) pula direto para o próximo modelo, sem novas tentativas.

### PR 2 — Flow + legenda + vídeo (pendente)

Decisões do usuário: o worker na VM gera o vídeo automaticamente para os produtos enviados, com teto diário de créditos configurável (padrão 75) e pausa se o custo exibido divergir. O vídeo legendado chega como resposta à mensagem do produto (`deliveries.photo_message_id`). A legenda vem de transcrição com faster-whisper local (modelo small, CPU), e o texto do roteiro (`analysis.narration`) só corrige a grafia.

## Estado do repositório

Arquivos não rastreados, não tocar por engano: daily_shopee_top5.ps1, docker-schedule.sh, get_telegram_chat_id.ps1, install_daily_task.ps1.
