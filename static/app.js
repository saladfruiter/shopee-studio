'use strict';
const $=s=>document.querySelector(s),money=n=>new Intl.NumberFormat('pt-BR',{style:'currency',currency:'BRL'}).format(n);
const date=s=>s?new Date(s).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state,csrf='',activeRun=null,allOffers=false,sendingRun=null,wasBusy=false;
const labels={ready:'Pronta',running:'Consultando',failed:'Falhou',not_sent:'Não enviado',sending:'Enviando',sent:'Confirmado',unknown:'Não confirmado',new:'Ainda não avaliado',planned:'Vou divulgar',published:'Já divulguei',discarded:'Descartar'};
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,6500);}
async function api(path,options={}){
 const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
 const result=await response.json();
 if(!response.ok){if(response.status===401){$('#app').hidden=true;$('#login').hidden=false;}throw Error(result.error||'Não foi possível concluir.');}
 return result;
}
const post=(path,value={})=>api(path,{method:'POST',body:JSON.stringify(value)});
function view(name){document.querySelectorAll('.view').forEach(e=>e.hidden=e.id!==name);document.querySelectorAll('[data-view]').forEach(e=>e.classList.toggle('active',e.dataset.view===name));$('#page-title').textContent={overview:'Seu próximo Top 5.',history:'Um radar com memória.',settings:'Do seu jeito.'}[name];$('#page-subtitle').textContent={overview:'Menos ruído. Mais espaço para escolher o que vale divulgar.',history:'Veja o que mudou, o que foi enviado e o que merece outra olhada.',settings:'Ajuste a seleção, sua rotina e o destino das ofertas.'}[name];}
document.querySelectorAll('[data-view]').forEach(e=>e.onclick=()=>view(e.dataset.view));
$('#login-form').onsubmit=async e=>{e.preventDefault();$('#login-error').textContent='';try{const r=await post('/login',{password:$('#password').value});csrf=r.csrf;$('#password').value='';await refresh(true);}catch(error){$('#login-error').textContent=error.message;}};
$('#logout').onclick=async()=>{try{await post('/logout');$('#app').hidden=true;$('#login').hidden=false;}catch(e){toast(e.message);}};
async function refresh(fillForms=false){
 state=await api('/state');csrf=state.csrf;$('#login').hidden=true;$('#app').hidden=false;
 $('#next-run').textContent=state.next_run?date(state.next_run):'Pausado';
 const last=state.runs[0],sent=state.runs.find(r=>r.delivery==='sent');
 $('#last-run').textContent=state.busy?'Consultando…':last?labels[last.status]:'Nenhuma';
 $('#last-run-detail').textContent=last?date(last.started)+' · '+last.scanned+' ofertas consultadas':'Seu radar está pronto para começar';
 $('#last-send').textContent=sent?date(sent.sent_at):'Ainda não enviado';$('#planned').innerHTML=(state.counts.planned||0)+' <em>produtos</em>';
 $('#version').textContent='Versão '+state.version.slice(0,12);$('#collect').disabled=state.busy;$('#collect').textContent=state.busy?'↻ Consultando…':'↻ Consultar agora';
 const warnings=[];
 if(!state.shopee_configured)warnings.push('Configure as credenciais da Shopee em Preferências para consultar ofertas.');
 if(state.settings.auto_send&&!state.telegram.configured)warnings.push('O envio automático está ligado, mas o Telegram ainda não está configurado.');
 if(state.schedule?.status==='failed')warnings.push('O agendamento falhou após três tentativas. Consulte os registros e use Consultar agora após corrigir.');
 if(state.schedule?.status==='unknown')warnings.push('Entrega não confirmada. Verifique o Telegram antes de enviar uma nova consulta.');
 $('#notice').textContent=warnings.join(' ');$('#notice').hidden=!warnings.length;
 if(!activeRun||wasBusy&&!state.busy)activeRun=state.latest;else if(state.latest?.id===activeRun.id)activeRun=state.latest;
 wasBusy=state.busy;renderOffers();
 $('#events').innerHTML=state.events.slice(0,5).map(e=>'<div class="event"><i class="event-dot '+esc(e.level)+'"></i><span>'+esc(e.message)+'</span><time>'+date(e.created)+'</time></div>').join('')||'<p class="muted">As atividades aparecerão aqui após a primeira consulta.</p>';
 $('#weights').innerHTML=[['commission_weight','Comissão'],['rating_weight','Avaliação'],['sales_weight','Vendas']].map(([k,v])=>'<div><strong>'+state.settings[k]+'%</strong><span>'+v+'</span></div>').join('');
 $('#history-body').innerHTML=state.runs.map(r=>'<tr><td>#'+r.id+' · '+date(r.started)+'</td><td>'+(r.source==='scheduled'?'Agendada':'Manual')+'</td><td>'+r.scanned+'</td><td>'+esc(labels[r.status])+'</td><td>'+esc(labels[r.delivery])+'</td><td><button class="secondary" data-run="'+r.id+'">Ver</button></td></tr>').join('')||'<tr><td colspan="6">Nenhuma consulta registrada.</td></tr>';
 document.querySelectorAll('[data-run]').forEach(b=>b.onclick=async()=>{try{activeRun=await api('/runs/'+b.dataset.run);allOffers=false;renderOffers();view('overview');}catch(e){toast(e.message);}});
 if(fillForms){$('#shopee-app-id').value=state.shopee_app_id||'';for(const [key,value] of Object.entries(state.settings)){const input=$('#settings-form').elements.namedItem(key);if(input){if(input.type==='checkbox')input.checked=value;else input.value=value;}}$('#telegram-chat').value=state.telegram.chat_id||'';$('#telegram-topic').value=state.telegram.thread_id||'';}
 $('#telegram-status').textContent=state.telegram.configured?'Bot configurado · chat '+state.telegram.chat_id+(state.telegram.thread_id?' · tópico '+state.telegram.thread_id:' · sem tópico específico'):'Configure seu bot e o destino. Nenhuma mensagem será enviada ao salvar.';
}
function renderOffers(){
 const s=activeRun?.settings||state.settings;
 $('#filter-summary').innerHTML=[s.keyword||'Todos os nichos','Nota ≥ '+s.min_rating,s.max_price?'Até '+money(s.max_price):'Sem teto de preço',s.repeat_days+' dias sem repetir',activeRun?'Consulta #'+activeRun.id:'Aguardando primeira consulta'].map(v=>'<span>'+esc(v)+'</span>').join('');
 $('#run-warning').textContent=activeRun?.error||activeRun?.warning||'';$('#run-warning').hidden=!$('#run-warning').textContent;
 const products=(activeRun?.products||[]).filter(p=>allOffers||p.selected);
 $('#show-all').textContent=allOffers?'Ver seleção':'Ver alternativas';$('#preview').disabled=!activeRun||activeRun.status!=='ready';
 if(!products.length){$('#offers').innerHTML='<div class="empty"><div class="symbol">✧</div><h2>Seu próximo achado começa aqui.</h2><p>Configure o nicho e a faixa de preço em Preferências. Depois, consulte as ofertas para montar sua seleção.</p></div>';return;}
 $('#offers').innerHTML=products.slice(0,200).map((p,i)=>'<article class="offer-card"><div class="card-top"><span class="rank">'+String(i+1).padStart(2,'0')+'</span><span class="score">'+(p.selected?'SELECIONADO':'ALTERNATIVA')+' · '+p.score.toFixed(1)+'</span></div><h3 title="'+esc(p.name)+'">'+esc(p.name)+'</h3><p class="shop">'+esc(p.shop)+'</p><div class="price-row"><strong>'+money(p.price)+'</strong><div class="commission"><small>COMISSÃO ESTIMADA</small><b>'+money(p.commission)+'</b></div></div><div class="offer-facts"><span>★ '+p.rating.toFixed(1)+'</span><span>'+p.sales.toLocaleString('pt-BR')+' vendas</span></div><div class="card-actions"><select aria-label="Status de '+esc(p.name)+'" data-key="'+esc(p.key)+'">'+['new','planned','published','discarded'].map(k=>'<option value="'+k+'" '+(p.decision===k?'selected':'')+'>'+labels[k]+'</option>').join('')+'</select><a href="'+esc(p.link)+'" target="_blank" rel="noopener noreferrer">Ver oferta ↗</a></div><button class="mini-button" data-product="'+esc(p.key)+'">Por que entrou? · Ver evolução</button></article>').join('');
 document.querySelectorAll('[data-key]').forEach(el=>el.onchange=async()=>{try{await post('/decisions',{key:el.dataset.key,state:el.value});activeRun=await api('/runs/'+activeRun.id);await refresh();toast('Triagem atualizada.');}catch(e){toast(e.message);}});
 document.querySelectorAll('[data-product]').forEach(el=>el.onclick=async()=>{try{const p=products.find(p=>p.key===el.dataset.product),h=await api('/products/'+encodeURIComponent(p.key)+'/history');$('#modal-title').textContent='Por que esta oferta?';$('#modal-destination').textContent=p.name;$('#modal-content').textContent=p.reasons.join('\n')+'\n\nEVOLUÇÃO OBSERVADA\n'+h.map(x=>date(x.started)+' · '+money(x.price)+' · comissão '+money(x.commission)).join('\n');$('#confirm-send').hidden=true;$('#modal').showModal();}catch(e){toast(e.message);}});
}
$('#show-all').onclick=()=>{allOffers=!allOffers;renderOffers();};
$('#collect').onclick=async()=>{try{await post('/collect');activeRun=null;toast('Consulta iniciada. Nenhuma mensagem será enviada por esta ação.');await refresh();}catch(e){toast(e.message);}};
$('#settings-form').onsubmit=async e=>{e.preventDefault();const r={};for(const[k,v]of Object.entries(state.settings)){const i=e.target.elements.namedItem(k);r[k]=typeof v==='boolean'?i.checked:typeof v==='number'?Number(i.value):i.value.trim();}try{await post('/settings',r);await refresh(true);toast('Preferências salvas.');}catch(error){toast(error.message);}};
$('#telegram-form').onsubmit=async e=>{e.preventDefault();const r={chat_id:$('#telegram-chat').value.trim(),thread_id:$('#telegram-topic').value.trim(),token:$('#telegram-token').value.trim()};try{await post('/telegram',r);$('#telegram-token').value='';await refresh(true);toast('Telegram salvo. Nenhuma mensagem foi enviada.');}catch(error){toast(error.message);}};
$('#preview').onclick=async()=>{try{sendingRun=activeRun.id;const p=await api('/runs/'+sendingRun+'/preview');$('#modal-title').textContent='Confira antes de enviar';$('#modal-destination').textContent='Destino: '+(p.destination.chat_id||'não configurado')+(p.destination.thread_id?' · tópico '+p.destination.thread_id:'');$('#modal-content').textContent=p.text;$('#confirm-send').hidden=false;$('#confirm-send').disabled=!p.can_send||!p.destination.configured;$('#modal').showModal();}catch(e){toast(e.message);}};
$('#confirm-send').onclick=async()=>{$('#confirm-send').disabled=true;try{await post('/runs/'+sendingRun+'/send');$('#modal').close();toast('Entrega confirmada pelo Telegram.');await refresh();}catch(e){toast(e.message);await refresh();}};
$('#close-modal').onclick=()=>$('#modal').close();
refresh(true).catch(()=>{});
setInterval(()=>{if(!$('#app').hidden&&!document.hidden)refresh().catch(e=>toast(e.message));},5000);

$('#shopee-form').onsubmit=async e=>{e.preventDefault();try{await post('/shopee',{app_id:$('#shopee-app-id').value.trim(),secret:$('#shopee-secret').value.trim()});$('#shopee-secret').value='';await refresh(true);toast('Shopee configurada.');}catch(error){toast(error.message);}};
