# Shopee Studio

Painel pessoal para pesquisar, selecionar e acompanhar ofertas de afiliados Shopee. Roda em um container no Unraid, com SQLite persistente e interface em português.

## O que faz

- Consulta a API oficial, trata respostas sem campo errors e repete falhas transitórias até três vezes.
- Filtra nicho, preço, avaliação, vendas e comissão; explica a classificação.
- Consulta produtos diretamente, sem rodízio ou filtro de categoria. Analisa até o limite de páginas configurado e classifica as ofertas encontradas por comissão, avaliação e vendas.
- Usa comissão estimada de cliente existente quando disponível, sem misturar taxas de novos clientes. Normalização logarítmica reduz a influência de valores extremos.
- Respeita o limite por loja na seleção.
- Exclui permanentemente das novas consultas os anúncios já enviados ao Telegram, identificados por loja e item. Uma entrega incerta também fica excluída para evitar um possível reenvio; produtos descartados ou já divulgados ficam excluídos até mudar sua triagem.
- Histórico de consultas, preços observados, decisões e entregas; SQLite persiste nas atualizações.
- Consulta manual apenas prepara a prévia. Envio manual exige confirmar no painel.
- Configuração do Telegram pela interface: token, chat/grupo e tópico (message_thread_id). Token vazio preserva o já salvo; tópico vazio remove o destino de tópico.
- Para cada produto selecionado, busca anúncios do mesmo produto (uma página extra da API por produto) e pede a um modelo de visão para comparar imagem e atributos: marca/modelo, variante/cor, tamanho/capacidade, acessórios e kit. Só os classificados como "mesmo produto" viram links; incertos e diferentes ficam de fora, com o motivo visível no painel. Cada produto sai com até 6 links: o selecionado e até 5 equivalentes, numa lista única.
- O mesmo modelo escreve a legenda (frase + 2 ou 3 hashtags, no máximo 150 caracteres no total) e o roteiro de 10 segundos (gancho, demonstração, benefício, chamada e narração). O código confere a contagem de caracteres (UTF-16), as hashtags proibidas (#Shopee, #Achadinhos, #Viral, #Fyp…), emojis, reticências e promessas de preço, desconto, frete ou urgência. Resposta fora das regras volta ao modelo para correção. Não há título de vídeo separado.
- Modelo configurável em Preferências: principal + fallbacks em ordem. Padrão de fábrica: Antigravity CLI (agy) com gemini-3.8-flash-medium e gemini-4-argon-medium como fallback, executados pelo worker da VM (veja worker/README.md). Também aceita endpoints OpenAI-compatíveis (OpenRouter, llama.cpp) com chave protegida, modelo visual e modelo de texto opcional. Ajuste ali o tempo limite, as novas tentativas e quantos candidatos são comparados. O botão Testar visão manda uma imagem real ao modelo. Se todos os modelos falharem, o produto sai com o próprio link e um texto automático marcado como "sem validação do modelo".
- No Telegram, cada produto vira uma mensagem com a foto do anúncio (o painel baixa a imagem original e envia ao Telegram, que aplica a compressão normal de fotos), a identificação, a legenda num bloco copiável e os links. O prompt do Google Flow chega em outro bloco copiável, como resposta a essa mensagem. Não há botões de cópia.
- Agenda diariamente em America/Sao_Paulo, recupera o horário perdido do dia ao reiniciar e tenta falhas novamente após 5 e 10 minutos.
- Entrega incerta não é repetida automaticamente: confira o Telegram. Não há garantia de exactly-once na API do Telegram.
- Login no painel, proteção CSRF e limitação de tentativas de login. Segredos ficam fora do Git e da imagem.

## Worker da VM

O Antigravity CLI só existe logado na VM hermes, então o painel não o chama direto. Um worker na VM busca as tarefas pelo endpoint /api/worker/*, autenticado por um token gerado em Preferências → Worker da VM, e devolve o resultado. Instalação e operação: worker/README.md. Sem o worker, os modelos agy são pulados na hora e a cadeia segue para os fallbacks.

## Unraid / Compose Manager

1. Crie uma stack chamada shopee-studio com o conteúdo de compose.yaml deste repositório.
2. Coloque um .env junto ao Compose, baseado em .env.example. Defina PANEL_PASSWORD. As credenciais da Shopee e do Telegram podem ser preenchidas depois pela interface.
3. Execute Compose Up. Acesse http://IP-DO-UNRAID:8787 e use a senha PANEL_PASSWORD.
4. Em Preferências, confira filtros, configure Telegram e o tópico. Faça uma consulta e revise a prévia. Anúncios já enviados pelo aplicativo ficam excluídos permanentemente.
5. Habilite envio automático quando o destino e o ranking estiverem corretos. Por padrão, só gera prévias.

A porta 8787 deve estar livre. PANEL_PORT altera a porta externa. Use BIND_IP para restringir à interface LAN desejada. Com proxy HTTPS, use COOKIE_SECURE=true e acesse apenas o endereço HTTPS. Não exponha diretamente a porta na internet.

O volume shopee-studio_shopee-data guarda banco e chave de sessão. Faça backup desse volume com o container parado. Ele também contém os segredos da Shopee e do Telegram configurados na interface; trate o backup como confidencial. Não use docker compose down -v ao atualizar.

## Atualizações GitHub → Unraid

Cada push na branch main executa os testes e publica:
- ghcr.io/saladfruiter/shopee-studio:latest
- ghcr.io/saladfruiter/shopee-studio:sha-COMMIT_COMPLETO

A stack de produção usa image:, sem build local. O Compose Manager pode comparar a imagem instalada com a imagem publicada e exibir atualização disponível ao usar Check for Updates. No Compose Manager Plus, habilite Auto Check for Updates e selecione Every Time para consultar ao abrir a página de stacks. Os demais intervalos limitam a frequência dessa consulta; a opção descreve uma checagem ao carregar a página, não um monitor em segundo plano. Uma alteração só fica disponível quando o workflow termina com sucesso.

O primeiro pacote GHCR precisa ter visibilidade Public em GitHub → Packages → shopee-studio → Package settings. Um repositório público sozinho não torna o pacote público. Depois disso, o servidor não precisa de login para baixar.

A atualização é manual: use Update Stack / Compose Up com pull, ou:
    docker compose pull
    docker compose up -d

O serviço não se atualiza sozinho. A interface exibe o SHA da versão instalada. Para reverter, use uma tag sha- anterior em image: e recrie o container; preserve o volume.

## Desenvolvimento e testes

Python 3.13:
    python -m venv .venv
    # Ative o ambiente virtual
    pip install -r requirements.txt
    python -m unittest discover -s tests -v
    # Copie .env.example para .env e preencha
    python app.py

Servidor WSGI Waitress, porta 8787. Com Docker local:
    docker compose -f compose.dev.yaml up --build

Os testes usam respostas simuladas e não enviam mensagens. Não existe senha padrão. Não publique .env, data/, arquivos zip antigos ou logs.

## Limites e operação

- A classificação é uma heurística configurável, não previsão de vendas ou comissão garantida.
- Concorrência de vídeos e quantidade de avaliações não são coletadas; valide manualmente.
- A Open API traz nome, uma imagem, loja e preços, mas não as especificações nem as variantes do anúncio. A comparação de equivalentes depende dessa imagem e do título. Por isso, na dúvida, o modelo deve marcar "incerto". Confira os links antes de vincular ao vídeo: a elegibilidade e a comissão no canal Vídeo não são verificadas.
- Anúncios equivalentes já enviados também ficam excluídos das próximas consultas, para não gerar outro vídeo do mesmo produto.
- Uma consulta com análise leva cerca de 1 minuto por produto com o agy. O painel mostra o progresso. Produtos analisados no momento de um reinício são descartados, e a consulta fica como falha.
- A coleta lê até o limite de páginas configurado, ordenadas por comissão. O ranking considera os produtos encontrados nesse intervalo, não o catálogo inteiro da Shopee. Aumente o limite ou use uma palavra-chave se faltarem anúncios inéditos.
- Histórico registra ofertas elegíveis observadas; não é histórico completo do catálogo.
- Logs recentes ficam limitados a 1.000 eventos. Consultas/ofertas são mantidas no SQLite para preservar histórico.
- Um único processo/container deve usar o banco. Não escale réplicas.
- Desative eventual tarefa antiga ShopeeAffiliateTop5Daily no Windows ao migrar para o Unraid, para evitar dois agendamentos.

## Scripts legados

Na cópia local original, daily_shopee_top5.ps1, docker-schedule.sh e install_daily_task.ps1 foram preservados; eles não fazem parte desta publicação nem são usados pelo novo container. O fluxo recomendado é o painel. Não execute os dois agendadores ao mesmo tempo.
