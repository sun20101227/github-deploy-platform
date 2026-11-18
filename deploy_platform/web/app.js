'use strict';
/* Deploy 控制台：哈希路由 + 原生 DOM，无构建步骤。discovery.js 共用这里的 $ / esc / api / busy 等函数。 */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stateNames = {queued:'排队中',running:'部署中',succeeded:'已完成',failed:'失败',rolled_back:'已恢复旧版',rollback_failed:'回滚失败'};
const actionNames = {deploy:'部署',update:'更新',rollback:'回滚',webhook:'自动更新',import:'接入'};
const ACTIVE = ['queued','running'];
let epoch = 0, poll;
let selectedJob = {project:0, job:0};   // 部署记录里选中的任务，轮询刷新时保持不变

/* ---- 小工具 ---- */
const stamp = s => s ? new Date(s).toLocaleString('zh-CN',{hour12:false}) : '—';
function ago(s){
  if(!s) return '—';
  const sec = Math.max(0, (Date.now() - new Date(s)) / 1000);
  if(sec < 45) return '刚刚';
  if(sec < 3600) return Math.round(sec/60) + ' 分钟前';
  if(sec < 86400) return Math.round(sec/3600) + ' 小时前';
  if(sec < 86400*30) return Math.round(sec/86400) + ' 天前';
  return new Date(s).toLocaleDateString('zh-CN');
}
function took(job){
  if(!job.started_at) return '—';
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const sec = Math.max(0, Math.round((end - new Date(job.started_at)) / 1000));
  return sec < 60 ? sec + ' 秒' : Math.floor(sec/60) + ' 分 ' + (sec % 60) + ' 秒';
}
const when = s => `<time datetime="${esc(s)}" title="${esc(stamp(s))}">${esc(ago(s))}</time>`;
const badge = s => `<span class="badge ${esc(s||'none')}">${stateNames[s] || '未部署'}</span>`;
function sha(repo, value, n=8){
  if(!value) return '<span class="muted">—</span>';
  return `<a class="sha" href="https://github.com/${esc(repo)}/commit/${esc(value)}" target="_blank" rel="noopener noreferrer" title="${esc(value)}">${esc(value.slice(0,n))}</a>`;
}
function toast(text){const t=$('#toast');t.hidden=true;t.textContent=text;void t.offsetWidth;t.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.hidden=true,4000)}
async function api(path, data, method){
  // 登录状态在 HttpOnly 的会话 Cookie 里，脚本读不到；改动类请求带上 X-Deploy-Request，别的网站伪造不了这个头
  const r = await fetch(path,{method:method || (data===undefined?'GET':'POST'),credentials:'same-origin',headers:{'Content-Type':'application/json','X-Deploy-Request':'1'},body:data===undefined?undefined:JSON.stringify(data)});
  let d = {};
  try { d = await r.json() } catch { /* 网关错误页不是 JSON */ }
  if(r.status===401 && !path.startsWith('/api/auth/')){ me = null; showUser(); location.hash='/login'; throw Error('请先登录') }
  if(!r.ok) throw Error(typeof d.detail==='string' ? d.detail : `请求失败（${r.status}），请检查输入或服务器日志`);
  return d;
}
function header(title,sub='',action=''){return `<header><div><h1>${esc(title)}</h1>${sub?`<p>${esc(sub)}</p>`:''}</div>${action?`<div class="buttons">${action}</div>`:''}</header>`}
function inlineError(e){const el=$('#error');if(el)el.textContent=e.message;else toast(e.message)}
async function busy(btn,fn){
  if(!btn) return fn().catch(inlineError);
  const label = btn.innerHTML;
  btn.disabled = true; btn.setAttribute('aria-busy','true');
  const slow = setTimeout(()=>{ if(btn.isConnected) btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>' + label }, 300);
  try { const err=$('#error'); if(err) err.textContent=''; await fn() } catch(e){ inlineError(e) }
  finally { clearTimeout(slow); if(btn.isConnected){ btn.innerHTML = label; btn.disabled = false; btn.removeAttribute('aria-busy') } }
}
async function confirmAction(title,copy,ok='确认'){
  const d=$('#confirm');$('#confirm-title').textContent=title;$('#confirm-copy').textContent=copy;$('#confirm-ok').textContent=ok;
  d.returnValue='cancel';d.showModal();
  return new Promise(resolve=>d.addEventListener('close',()=>resolve(d.returnValue==='ok'),{once:true}));
}
function empty(title,copy,link,label){return `<div class="sheet empty"><svg viewBox="0 0 40 40" aria-hidden="true"><rect x="5" y="10" width="30" height="24" rx="3"/><path d="M5 17h30M12 10V6h12l5 4M16 25h8"/></svg><h2>${title}</h2><p>${copy}</p>${link?`<a class="button primary" href="${link}">${label}</a>`:''}</div>`}
const skeleton = () => '<div aria-hidden="true"><span class="sk sk-title"></span><span class="sk sk-sub"></span><div class="sheet"><span class="sk sk-row"></span><span class="sk sk-row"></span><span class="sk sk-row"></span></div></div><p class="loading" role="status" hidden>正在读取…</p>';
async function copy(text){try{await navigator.clipboard.writeText(text);toast('已复制')}catch{toast('复制失败，请手动选中文字')}}

/* ---- 部署步骤：从任务记录里平台写下的步骤行推算进度 ---- */
const STEP_LINES = ['拉取代码','拉取远端提交','准备回滚版本','执行构建','切换版本，启动服务','重启服务并检查状态'];
const OK_LINES = /^(健康检查通过|服务已重启|更新与重启完成|已恢复|已清理|已登记)/;
const BAD_LINES = /失败|未通过|中断|无法|超过|不符合|已改变|已分叉|请先|不存在/;
function stepsFor(project, job){
  if(!job || job.action==='import') return '';
  // A project that is not a Git checkout has no fetch step: the platform only builds and restarts.
  const names = project.mode==='external' ? [] : [job.action==='rollback' ? '准备旧版本' : '拉取代码'];
  if(project.build_command) names.push('构建');
  names.push(project.healthcheck_url ? '重启并检查健康' : '重启服务');
  const msg = job.message || '';
  let reached = job.status==='queued' ? -1 : 0;
  if(/执行构建/.test(msg) && project.build_command) reached = names.indexOf('构建');
  if(/切换版本|重启服务/.test(msg)) reached = names.length - 1;
  const done = job.status==='succeeded';
  const failed = !ACTIVE.includes(job.status) && !done;
  return `<ol class="steps" aria-label="部署进度">${names.map((n,i)=>{
    const cls = done || i < reached ? 'done' : i === reached ? (failed ? 'bad' : 'now') : '';
    return `<li class="${cls}"${cls==='now'?' aria-current="step"':''}><i aria-hidden="true"></i>${esc(n)}</li>`;
  }).join('')}</ol>`;
}
function terminal(text){
  const lines = String(text||'').replace(/\n+$/,'').split('\n');
  return `<pre class="terminal" id="log" tabindex="0" aria-label="任务输出">${lines.map(l=>{
    const t = l.trim();
    const cls = STEP_LINES.includes(t) ? 'step' : OK_LINES.test(t) ? 'ok' : BAD_LINES.test(t) ? 'bad' : '';
    return `<span class="ln ${cls}">${esc(l) || ' '}</span>`;
  }).join('') || '<span class="ln dim">等待执行…</span>'}</pre>`;
}
function runtimeText(p){
  const s = JSON.parse(p.runtime_config || '{}');
  if(p.runtime==='compose') return `Docker Compose · ${esc(s.project || '仓库名')}${s.files?.length ? `<br><small>${esc(s.files.map(f=>f.split(/[\\/]/).pop()).join('、'))}</small>` : ''}`;
  if(p.runtime==='systemd') return `systemd · ${esc(s.unit)}`;
  return `自定义命令<br><code>${esc(p.start_command || '—')}</code>`;
}

/* ---- 项目列表 ---- */
async function projectsPage(t){
  const projects = await api('/api/projects');
  const jobs = await Promise.all(projects.map(p=>api(`/api/projects/${p.id}/deployments?limit=1`)));
  if(t!==epoch) return;
  const rows = projects.map((p,i)=>({...p,job:jobs[i][0]}));
  const active = rows.some(p=>ACTIVE.includes(p.job?.status));
  $('#nav-count').textContent = projects.length || '';
  $('#nav-count').className = active ? 'nav-live' : '';
  $('#nav-count').title = active ? '有部署正在进行' : '';
  const query = $('#search')?.value || '';
  $('#content').innerHTML = header('项目',`${projects.length} 个项目${active?' · 有部署正在进行':''}`,'<a class="button primary" href="#/new">新建项目</a>') +
    (projects.length ? '<div class="toolbar"><input type="search" id="search" aria-label="搜索项目" placeholder="搜索仓库或分支"><span>当前服务器</span></div><div class="sheet" id="project-list"></div>'
      : empty('还没有项目','先连接 GitHub，再新建项目或接入服务器上已部署的项目。','#/discovery','查看已有项目'));
  function render(){
    const q = ($('#search')?.value||'').toLowerCase();
    const list = rows.filter(p=>(p.repository+' '+p.branch).toLowerCase().includes(q));
    $('#project-list').innerHTML = list.map((p,i)=>`<a class="project-row" style="--i:${i}" href="#/project/${p.id}">
      <div><strong>${esc(p.repository.split('/').pop())}${p.mode==='existing'||p.mode==='external'?'<span class="kind">已接入</span>':''}</strong><small>${esc(p.repository)} · ${esc(p.branch)}</small></div>
      <div>${badge(p.job?.status)}</div><span class="when">${p.job ? when(p.job.finished_at || p.job.started_at) : '尚未部署'}</span>
      <code class="commit">${esc(p.job?.commit_sha?.slice(0,8)||'—')}</code><span class="arrow" aria-hidden="true">↗</span></a>`).join('') || '<div class="empty">没有匹配的项目</div>';
  }
  if(projects.length){ $('#search').value = query; render(); $('#search').oninput = render }
  if(active) poll = setTimeout(()=>{ if(epoch===t) projectsPage(t).catch(()=>{}) }, 4000);
}

/* ---- GitHub ---- */
async function githubPage(t){
  // accounts connects a logged-in gh by itself; status says what gh itself reports
  const [accounts, gh] = await Promise.all([api('/api/accounts'), api('/api/github/status')]); if(t!==epoch) return;
  const live = gh.connected ? accounts.find(a=>a.github_id===gh.github_id) : null;
  $('#content').innerHTML = header('GitHub','使用服务器上的 GitHub CLI 账号。') + `<div class="narrow">
    <section class="card hero ${gh.connected?'ok':'bad'}"><h2>服务器上的 gh</h2>${gh.connected
      ? `<p class="state-line"><span class="badge succeeded">已登录</span> ${esc(gh.login)}${live?'，已用于读取仓库和拉取代码。':'。'}</p>`
      : `<p class="state-line"><span class="badge failed">未登录</span></p><p class="error">${esc(gh.message||'读不到 gh 的登录状态')}</p>`}</section>
    <section class="section"><h2>已连接账号</h2>${accounts.length?`<div class="sheet">${accounts.map((a,i)=>`<div class="account-line" style="--i:${i}"><span class="avatar">${esc(a.login[0].toUpperCase())}</span><div><strong>${esc(a.login)}</strong><small>${gh.connected&&a.github_id===gh.github_id?'当前 gh 登录的账号':'gh 当前没有登录这个账号，拉取代码会失败'}</small></div><span class="badge ${gh.connected&&a.github_id===gh.github_id?'succeeded':'failed'}">${gh.connected&&a.github_id===gh.github_id?'可用':'不可用'}</span></div>`).join('')}</div>`:'<p>还没有账号。gh 登录后打开这个页面会自动连接。</p>'}</section>
    <section class="section"><h2>${gh.connected?'换一个账号':'登录 gh'}</h2><p>在服务器终端，用运行平台的系统用户执行：</p><div class="command"><code>gh auth login --hostname github.com --web</code><button id="copy-login" class="text">复制</button></div><p>完成浏览器授权后，点击下面的按钮。</p><button id="connect" class="${gh.connected&&live?'':'primary'}">重新读取 gh 账号</button><p id="error" class="error" role="alert"></p></section></div>`;
  $('#copy-login').onclick = ()=>copy('gh auth login --hostname github.com --web');
  $('#connect').onclick = e=>busy(e.currentTarget,async()=>{const a=await api('/api/github/connect',{});toast(`已连接 ${a.login}`);route()});
}

/* ---- 新建项目与设置表单 ---- */
function releaseRuntime(p={}){const s=JSON.parse(p.runtime_config||'{}');const r=p.runtime||'compose';return `<label>部署后重启方式<select name="runtime"><option value="compose" ${r==='compose'?'selected':''}>Docker Compose（自动构建并重启）</option><option value="command" ${r==='command'?'selected':''}>自定义命令</option></select><small>每次部署、自动更新和回滚后都会重启服务。</small></label><div data-runtime="compose"><label>Compose 项目名<input name="compose_project" data-optional value="${esc(s.project||'')}" placeholder="留空使用仓库名" pattern="[a-z0-9][a-z0-9_\\-]*"><small>每次部署沿用同一个项目名，不会另建一套容器和数据卷。</small></label><label>Compose 文件<input name="compose_files" data-optional value="${esc((s.files||[]).join(','))}" placeholder="留空自动识别 compose.yaml"><small>仓库内的相对路径。部署目录下的 .env 会自动传给 Compose；持久数据请用命名卷或版本目录之外的路径。</small></label></div>`}
function fields(p={},edit=false){const inPlace=p.mode==='existing'||p.mode==='external';return `<label>分支<input name="branch" required value="${esc(p.branch||'main')}" maxlength="200"></label>${!edit?'<label>部署目录<input name="deploy_root" required placeholder="/opt/apps/my-app"><small>此服务器上的绝对路径。</small></label>':''}<label>构建命令<input name="build_command" value="${esc(p.build_command||'')}" placeholder="例如 npm ci && npm run build"><small>${inPlace?'在原项目目录执行，可留空。':'在新版本目录执行，可留空。'}</small></label>${inPlace?'':releaseRuntime(p)}<label>${inPlace?'重启命令':'启动 / 重启命令'}<input name="start_command" required value="${esc(p.start_command||'')}" placeholder="例如 pm2 restart my-app --update-env"><small>使用进程管理器重启服务，完成后退出。${inPlace?'在原项目目录执行。':''}</small></label><label>健康检查地址<input name="healthcheck_url" type="url" value="${esc(p.healthcheck_url||'')}" placeholder="http://127.0.0.1:3000/health"><small>返回 2xx 才算通过；留空时只检查重启是否完成。</small></label><label>健康检查最长等待（秒）<input name="healthcheck_timeout" type="number" required min="5" max="600" value="${esc(p.healthcheck_timeout||45)}"><small>超过这个时间仍未通过就判定失败并恢复旧版本；启动慢的服务可以调大。</small></label>`}
async function newPage(t){
  const accounts = await api('/api/accounts'); if(t!==epoch) return;
  $('#content').innerHTML = '<a class="back" href="#/projects">← 项目</a>' + header('新建项目','从 GitHub 仓库部署到这台服务器，每次部署是一个独立版本，可以回滚。') + (accounts.length ? `<form id="new-form" class="form"><div class="step-label">01 / 代码来源</div><div class="pair"><label>账号<select name="account_id" id="account">${accounts.map(a=>`<option value="${a.id}">${esc(a.login)}</option>`).join('')}</select></label><label>仓库<select name="repository" id="repository" required disabled><option value="">正在加载…</option></select></label></div><div class="section"><div class="step-label">02 / 运行配置</div>${fields()}</div><p id="error" class="error" role="alert"></p><div class="buttons"><button class="primary" id="save">创建项目</button><a class="button" href="#/projects">取消</a></div></form>` : empty('先连接 GitHub','连接后可以选择公开和私有仓库。','#/github','连接 GitHub'));
  if(!accounts.length) return;
  let request = 0;
  async function repos(){
    const id = ++request; const sel = $('#repository');
    sel.disabled = true; sel.innerHTML = '<option value="">正在加载…</option>';
    try {
      const rs = await api(`/api/accounts/${$('#account').value}/repositories`);
      if(t!==epoch || id!==request) return;
      sel.innerHTML = `<option value="">选择仓库（${rs.length} 个）</option>` + rs.map(r=>`<option data-branch="${esc(r.default_branch)}" value="${esc(r.full_name)}">${esc(r.full_name)}${r.private?' · 私有':''}</option>`).join('');
      sel.disabled = false;
    } catch(e) { if(t===epoch){ sel.innerHTML='<option value="">加载失败，重新选择账号后重试</option>'; inlineError(e) } }
  }
  $('#account').onchange = repos;
  $('#repository').onchange = ()=>{ $('[name=branch]').value = $('#repository').selectedOptions[0]?.dataset.branch || 'main' };
  $('#new-form').onsubmit = e=>{ e.preventDefault(); busy($('#save'),async()=>{ const d=Object.fromEntries(new FormData(e.target)); d.account_id=Number(d.account_id); d.runtime_config=readRuntime(e.target); const p=await api('/api/projects',d); location.hash=`/project/${p.id}`; toast('项目已创建，点“首次部署”开始') }) };
  bindRuntime($('#new-form')); repos();
}

/* ---- 项目详情 ---- */
async function projectPage(id,tab,t){
  const [p,jobs] = await Promise.all([api(`/api/projects/${id}`),api(`/api/projects/${id}/deployments`)]);
  if(t!==epoch) return;
  const last = jobs[0], active = jobs.find(j=>ACTIVE.includes(j.status));
  const current = jobs.find(j=>j.status==='succeeded');
  const root = `#/project/${id}`;
  const validTab = ['overview','history','settings'].includes(tab) ? tab : 'overview';
  const existing = p.mode==='existing', external = p.mode==='external';
  const action = existing || external ? '更新' : current ? '部署最新版本' : '首次部署';
  $('#content').innerHTML = `<a class="back" href="#/projects">← 项目</a>` +
    header(p.repository.split('/').pop(), `${p.repository} · ${p.branch}`, `<button class="primary" id="deploy" ${active?'disabled':''}>${active?'<span class="spinner" aria-hidden="true"></span>部署中':action}</button>`) +
    `<nav class="tabs" aria-label="项目导航">${[['overview','概览'],['history','部署记录',jobs.length],['settings','设置']].map(([key,label,n])=>`<a class="${validTab===key?'active':''}" href="${root}/${key}"${validTab===key?' aria-current="page"':''}>${label}${n?`<span class="count">${n}</span>`:''}</a>`).join('')}</nav><p id="error" class="error" role="alert"></p><div id="project-body"></div>`;
  // 先确认、再进入加载状态：等人决定的时候按钮不该转圈
  $('#deploy').onclick = async e=>{
    const btn = e.currentTarget;
    const copyText = external ? '这个目录不是 Git 仓库：将执行构建命令和重启（或项目自己的更新命令），然后检查服务状态。'
      : existing ? `将在原目录拉取 ${p.branch} 分支并快进合并，构建后重启服务。` : `将拉取 ${p.branch} 分支到新版本目录，构建完成后切换版本并重启服务。`;
    if(!await confirmAction(action, copyText, action)) return;
    busy(btn,async()=>{
    const r = await api(`/api/projects/${id}/deploy`,{});
    selectedJob = {project:id, job:r.deployment_id};
    location.hash = `/project/${id}/history`;
    });
  };
  const body = $('#project-body');

  if(validTab==='overview'){
    const canRollback = !!(current?.previous_release || current?.previous_commit) && !active;
    body.innerHTML = `<div class="split"><div>
      ${active ? `<section class="card hero new"><h2>正在${actionNames[active.action]||'部署'} · #${active.id}</h2>${stepsFor(p,active)}<p class="state-line"><span class="spinner" aria-hidden="true"></span>已进行 ${took(active)}，完成后自动刷新。</p><a class="button" href="${root}/history">查看输出</a></section>` : ''}
      <section class="card"><h2>当前版本</h2><dl class="kv"><dt>提交</dt><dd>${current ? sha(p.repository,current.commit_sha,12) : '<span class="muted">尚未部署</span>'}</dd><dt>分支</dt><dd>${esc(p.branch)}</dd><dt>部署时间</dt><dd>${current ? `${when(current.finished_at)} <small>· ${esc(stamp(current.finished_at))}</small>` : '—'}</dd><dt>最近任务</dt><dd>${badge(last?.status)}${last && last!==current ? ` <small>${when(last.started_at)}</small>` : ''}</dd></dl>
        <div class="buttons"><button id="check">检查更新</button><button id="rollback" ${canRollback?'':'disabled'}>回滚上一版</button></div><div id="check-result" class="settings-note" role="status"></div></section>
    </div><div class="card info"><h3>${existing?'原目录 · 已接入':external?'原目录 · 已接入（不是 Git 仓库）':'部署目录'}</h3><p class="mono">${esc(p.deploy_root)}</p><h3>重启方式</h3><p>${runtimeText(p)}</p><h3>健康检查</h3><p class="muted">${esc(p.healthcheck_url||'未配置')}${p.healthcheck_url?` <small>· 最长 ${esc(p.healthcheck_timeout||45)} 秒</small>`:''}</p></div></div>`;
    $('#check').onclick = e=>busy(e.currentTarget,async()=>{
      const d = await api(`/api/projects/${id}/check`);
      // Not a Git checkout: the platform cannot tell which commit is running, so do not claim one.
      const unknown = external && !d.current_sha;
      $('#check-result').innerHTML = `<div class="versions${d.available&&!unknown?' new':''}"><div><span>当前部署</span><strong>${esc(d.current_sha?.slice(0,7)||'—')}</strong>${unknown?'<small>由项目自己管理</small>':''}</div><span class="to" aria-hidden="true">${unknown?'·':d.available?'→':'='}</span><div><span>GitHub 上最新</span><strong>${esc(d.sha.slice(0,7))}</strong><small title="${esc(d.message)}">${esc(d.message)}</small></div></div><p class="state-line">${unknown?'这个目录不是 Git 仓库，看不出正在运行哪个提交；点“更新”会执行更新命令。':d.available?'有新提交可以部署。':'已经是最新版本。'}${d.date?` <small>${when(d.date)}提交</small>`:''}</p>`;
    });
    $('#rollback').onclick = async e=>{
      const btn = e.currentTarget;
      if(!await confirmAction('回滚上一版','将切换回上一版代码并重新启动服务。数据库不会回退。','回滚')) return;
      busy(btn,async()=>{
        const r = await api(`/api/projects/${id}/rollback`,{});
        selectedJob = {project:id, job:r.deployment_id};
        location.hash = `/project/${id}/history`;
      });
    };
  }

  if(validTab==='history'){
    if(!jobs.length){ body.innerHTML = empty('暂无部署记录','首次部署后，这里会显示每一步的结果和输出。'); }
    else {
      if(selectedJob.project!==id || !jobs.some(j=>j.id===selectedJob.job)) selectedJob = {project:id, job:last.id};
      body.innerHTML = `<div class="history-wrap"><table class="history"><thead><tr><th>任务</th><th>状态</th><th>提交</th><th>开始</th><th>用时</th></tr></thead><tbody>${jobs.map((j,i)=>`<tr style="--i:${i}" data-job="${j.id}" tabindex="0" aria-selected="${j.id===selectedJob.job}"><td>#${j.id} ${actionNames[j.action]||'部署'}</td><td>${badge(j.status)}</td><td>${sha(p.repository,j.commit_sha)}</td><td>${when(j.started_at)}</td><td>${j.action==='import'?'—':esc(took(j))}</td></tr>`).join('')}</tbody></table></div><section id="job-detail"></section>`;
      const show = ()=>{
        const j = jobs.find(x=>x.id===selectedJob.job);
        body.querySelectorAll('tr[data-job]').forEach(tr=>tr.setAttribute('aria-selected', String(Number(tr.dataset.job)===j.id)));
        $('#job-detail').innerHTML = `<div class="log-head"><h3>任务 #${j.id} · ${actionNames[j.action]||'部署'}</h3><span>${esc(stamp(j.started_at))}${j.finished_at?` · 用时 ${esc(took(j))}`:''}</span></div>${stepsFor(p,j)}${terminal(j.message)}`;
        if(ACTIVE.includes(j.status)){ const log=$('#log'); log.scrollTop = log.scrollHeight }
      };
      body.querySelectorAll('tr[data-job]').forEach(tr=>{
        const pick = ()=>{ selectedJob = {project:id, job:Number(tr.dataset.job)}; show() };
        tr.onclick = e=>{ if(!e.target.closest('a')) pick() };
        tr.onkeydown = e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); pick() } };
      });
      show();
    }
  }

  if(validTab==='settings'){
    body.innerHTML = `<form class="form" id="settings"><h2>运行配置</h2>${existing||external?runtimeFields(p.runtime,JSON.parse(p.runtime_config||'{}')):''}${fields(p,true)}<div class="buttons"><button class="primary" ${active?'disabled':''}>保存配置</button>${active?'<small>部署进行中，结束后才能修改。</small>':''}</div></form>
      <details class="narrow"><summary>自动更新 · GitHub Webhook</summary><dl class="kv"><dt>Payload URL</dt><dd><code>${esc(location.origin+'/webhooks/github/'+id)}</code> <button class="text" id="copy-hook" type="button">复制</button></dd><dt>Content type</dt><dd>application/json</dd><dt>事件</dt><dd>Push</dd></dl><button id="secret" type="button">显示 Secret</button><pre id="secret-value" hidden></pre></details>`;
    $('#copy-hook').onclick = ()=>copy(location.origin+'/webhooks/github/'+id);
    $('#secret').onclick = ()=>{ const v=$('#secret-value'); v.hidden=!v.hidden; v.textContent=p.webhook_secret; $('#secret').textContent=v.hidden?'显示 Secret':'隐藏 Secret' };
    $('#settings').onsubmit = e=>{ e.preventDefault(); busy(e.submitter,async()=>{ await api(`/api/projects/${id}`,{...p,...Object.fromEntries(new FormData(e.target)),runtime_config:readRuntime(e.target)},'PUT'); toast('已保存') }) };
    bindRuntime($('#settings'));
  }
  // 部署进行中：概览和记录每 2.5 秒刷新；设置页不刷新，免得冲掉正在填的内容
  if(active && validTab!=='settings') poll = setTimeout(()=>{ if(epoch===t) projectPage(id,tab,t).catch(inlineError) }, 2500);
}

/* ---- 平台更新：参考 qiyu-diary 的“系统更新”，先看正在运行和 GitHub 最新的差别 ---- */
async function platformPage(t){
  const d = await api('/api/self-update'); if(t!==epoch) return;
  $('#content').innerHTML = header('平台更新','更新 Deploy 自身。') + `<div class="narrow">
    <section class="card hero ${d.error?'bad':'ok'}" id="self-hero"><h2>版本</h2>
      <div class="versions" id="self-versions"><div><span>正在运行</span><strong>${esc(d.commit?.slice(0,7)||'—')}</strong><small>${esc(d.branch||'—')}</small></div><span class="to" aria-hidden="true">=</span><div><span>GitHub 上最新</span><strong>—</strong><small>还没有检查</small></div></div>
      <p class="state-line" id="self-result" role="status">${esc(d.message||'点“检查更新”读取 GitHub 上的最新提交。')}</p>
      <p id="error" class="error" role="alert">${esc(d.error||'')}</p>
      <div class="buttons"><button id="self-check">检查更新</button><button class="primary" id="self-apply" disabled>更新并重启</button></div></section>
    <section class="card"><h2>更新后重启</h2><p>${d.automatic_restart?'更新后自动重启，页面会等服务恢复后刷新。正在部署其他项目时不能更新。':'当前没有用守护方式启动，更新后不能自动重启。请改用 python -m deploy_platform 启动后再更新。'}</p>
      <dl class="kv"><dt>仓库</dt><dd class="mono">${esc(d.remote||'—')}</dd><dt>提交</dt><dd class="mono">${esc(d.commit||'—')}</dd></dl>
      <details><summary>更新条件</summary><p>需要 Git 克隆安装、干净的工作区，以及服务用户的 gh 登录。分支发生分叉时会停止更新。</p></details></section></div>`;
  $('#self-check').onclick = e=>busy(e.currentTarget,async()=>{
    const r = await api('/api/self-update/check',{});
    $('#self-versions').className = 'versions' + (r.available?' new':'');
    $('#self-versions').innerHTML = `<div><span>正在运行</span><strong>${esc(r.commit.slice(0,7))}</strong><small>${esc(r.branch)}</small></div><span class="to" aria-hidden="true">${r.available?'→':'='}</span><div><span>GitHub 上最新</span><strong>${esc(r.latest_commit.slice(0,7))}</strong><small>刚刚检查</small></div>`;
    $('#self-hero').className = 'card hero ' + (r.available?'new':'ok');
    $('#self-result').textContent = r.available ? '有新版本可以更新。' : '已经是最新版本。';
    $('#self-apply').disabled = !r.available || !d.automatic_restart;
  });
  $('#self-apply').onclick = async e=>{
    const btn = e.currentTarget;
    if(!await confirmAction('更新并重启平台','更新后会自动重启，页面会断开十几秒。','更新')) return;
    busy(btn,async()=>{
      const r = await api('/api/self-update/apply',{});
      $('#self-result').innerHTML = '<span class="spinner" aria-hidden="true"></span>代码已更新，正在重启…';
      await waitForRestart(r.instance);
    });
  };
}
async function waitForRestart(instance){
  const t = epoch; const deadline = Date.now() + 180000;
  while(Date.now() < deadline && epoch===t){
    await new Promise(r=>setTimeout(r,2000));
    try { const res = await fetch('/health',{cache:'no-store',signal:AbortSignal.timeout(3000)}); const h = await res.json(); if(h.instance && h.instance!==instance){ toast('平台已重启'); await route(); return } } catch {}
  }
  if(epoch===t) $('#self-result').textContent = '还没确认服务恢复，请检查服务器日志。';
}

/* ---- 登录、首次创建管理员、账户 ---- */
let me = null;   // 当前登录的管理员用户名；没登录时为 null
function showUser(){
  $('#who').textContent = me || '未登录';
  $('#lock').hidden = !me;
}
async function loginPage(t){
  const s = await api('/api/auth/state'); if(t!==epoch) return;
  if(s.logged_in){ me = s.username; showUser(); location.hash = '/projects'; return }
  if(!s.admin_exists){
    $('#content').innerHTML = header('创建管理员账户','第一次使用，先创建登录控制台用的账户。') + (s.setup_allowed
      ? `<form id="setup" class="form card narrow">${s.setup_needs_token?'<label>管理密钥<input type="password" name="token" required autocomplete="off"><small>服务器 .env 里的 DEPLOY_ADMIN_TOKEN，用来确认是你在操作。只需要这一次。</small></label>':''}
          <label>用户名<input name="username" required minlength="3" maxlength="32" pattern="[A-Za-z0-9_.\\-]+" autocomplete="username" value="admin"><small>英文字母、数字、点、横线和下划线，3–32 个字符。</small></label>
          <label>密码<input type="password" name="password" required minlength="10" maxlength="128" autocomplete="new-password"><small>至少 10 个字符。</small></label>
          <label>再输一次密码<input type="password" name="confirm" required autocomplete="new-password"></label>
          <p id="error" class="error" role="alert"></p><button class="primary">创建并登录</button></form>`
      : `<div class="card narrow"><p>服务器没有设置 DEPLOY_ADMIN_TOKEN，只能在服务器上创建管理员：</p><div class="command"><code>python -m deploy_platform admin admin</code></div><p>在平台目录下用它的 Python 运行，按提示输入密码，然后刷新这个页面。</p></div>`);
    const f = $('#setup');
    if(f) f.onsubmit = e=>{ e.preventDefault(); busy(e.submitter,async()=>{
      const d = Object.fromEntries(new FormData(f));
      if(d.password!==d.confirm) throw Error('两次输入的密码不一样');
      const r = await api('/api/auth/setup',{username:d.username,password:d.password,token:d.token||''});
      me = r.username; showUser(); toast('管理员账户已创建'); location.hash = '/projects';
    }) };
    return;
  }
  $('#content').innerHTML = header('登录','用管理员账户登录控制台。') + `<form id="login" class="form card narrow">
    <label>用户名<input name="username" required autocomplete="username" autofocus></label>
    <label>密码<input type="password" name="password" required autocomplete="current-password"></label>
    <p id="error" class="error" role="alert"></p><button class="primary">登录</button>
    <p class="settings-note">忘记密码时，在服务器上运行 <code>python -m deploy_platform admin 用户名</code> 重新设置。</p></form>`;
  $('#login').onsubmit = e=>{ e.preventDefault(); busy(e.submitter,async()=>{
    const d = Object.fromEntries(new FormData(e.target));
    const r = await api('/api/auth/login',{username:d.username,password:d.password});
    me = r.username; showUser(); location.hash = '/projects';
  }) };
}
async function accountPage(t){
  const s = await api('/api/auth/state'); if(t!==epoch) return;
  $('#content').innerHTML = header('账户', s.logged_in ? `已登录为 ${s.username}` : '') + `<div class="narrow">
    ${s.logged_in ? `<form id="password" class="form card"><h2>修改密码</h2>
      <label>当前密码<input type="password" name="current" required autocomplete="current-password"></label>
      <label>新密码<input type="password" name="new" required minlength="10" maxlength="128" autocomplete="new-password"><small>至少 10 个字符。改完后其他设备上的登录会失效。</small></label>
      <label>再输一次新密码<input type="password" name="confirm" required autocomplete="new-password"></label>
      <p id="error" class="error" role="alert"></p><div class="buttons"><button class="primary">修改密码</button><button type="button" id="logout">退出登录</button></div></form>` : ''}
    <section class="card"><h2>脚本和 CI 怎么访问</h2><p>控制台用账户登录。脚本、CI 调用接口时仍然用服务器 .env 里的 DEPLOY_ADMIN_TOKEN，放在 <code>Authorization: Bearer</code> 头里，见接入文档。没有设置这个变量时，只有登录后的控制台能调用接口。</p></section></div>`;
  const f = $('#password');
  if(f){
    f.onsubmit = e=>{ e.preventDefault(); busy(e.submitter,async()=>{
      const d = Object.fromEntries(new FormData(f));
      if(d.new!==d.confirm) throw Error('两次输入的新密码不一样');
      await api('/api/auth/password',{current:d.current,new:d.new});
      f.reset(); toast('密码已修改，其他设备需要重新登录');
    }) };
    $('#logout').onclick = logout;
  }
}
async function logout(){
  try { await api('/api/auth/logout',{}) } catch {}
  me = null; showUser(); location.hash = '/login';
}

/* ---- 路由：换页时显示骨架、播一次进场；轮询刷新不重播 ---- */
async function route(){
  clearTimeout(poll); const t = ++epoch;
  const parts = location.hash.replace(/^#\/?/,'').split('/');
  const page = parts[0] || 'projects';
  const name = {projects:'项目',project:'项目',new:'新建项目',github:'GitHub',platform:'平台更新',login:'登录',discovery:'已有项目',account:'账户'}[page] || '项目';
  if(me===null && page!=='login'){
    // 第一次打开：问一下有没有登录，用来显示侧栏的用户名；没登录就去登录页
    try { const s = await api('/api/auth/state'); me = s.logged_in ? s.username : null } catch {}
    showUser();
    if(!me && t===epoch){ location.hash = '/login'; return }
  }
  document.title = name + ' · Deploy'; $('#crumb').textContent = name;
  document.querySelectorAll('[data-nav]').forEach(a=>{ if(a.dataset.nav===(['project','new'].includes(page)?'projects':page)) a.setAttribute('aria-current','page'); else a.removeAttribute('aria-current') });
  const content = $('#content');
  content.classList.remove('enter'); content.innerHTML = skeleton();
  try {
    if(page==='discovery') await discoveryPage(t);
    else if(page==='github') await githubPage(t);
    else if(page==='new') await newPage(t);
    else if(page==='project') await projectPage(Number(parts[1]),parts[2]||'overview',t);
    else if(page==='platform') await platformPage(t);
    else if(page==='login') await loginPage(t);
    else if(page==='account') await accountPage(t);
    else await projectsPage(t);
  } catch(e) {
    if(t===epoch){ content.innerHTML = header(name) + `<p class="error" role="alert">${esc(e.message)}</p><button id="retry">重试</button>`; $('#retry').onclick = route }
  }
  if(t===epoch){ content.classList.add('enter'); setTimeout(()=>{ if(t===epoch) content.classList.remove('enter') }, 450) }
}
$('#lock').onclick = logout;
window.addEventListener('hashchange',route); route();
