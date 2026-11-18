function runtimeFields(runtime = 'command', settings = {}) {
  return `<label>运行方式<select name="runtime">
    ${[['command','自定义重启命令'],['compose','Docker Compose'],['systemd','systemd']].map(([v,label])=>`<option value="${v}" ${runtime===v?'selected':''}>${label}</option>`).join('')}
  </select></label>
  <div data-runtime="compose"><label>原 Compose 项目名<input name="compose_project" value="${esc(settings.project||'')}"><small>沿用原项目名，避免创建另一套容器和卷。</small></label>
  <label>Compose 文件<input name="compose_files" value="${esc((settings.files||[]).join(','))}"><small>多个文件用英文逗号分隔，顺序与原部署一致。</small></label></div>
  <label data-runtime="systemd">systemd 服务名<input name="unit" value="${esc(settings.unit||'')}" placeholder="my-app.service"></label>`;
}

function bindRuntime(form) {
  const select = form.querySelector('[name=runtime]');
  function toggle() {
    form.querySelectorAll('[data-runtime]').forEach(el=>el.hidden=el.dataset.runtime!==select.value);
    const command = form.querySelector('[name=start_command]');
    command.required = select.value==='command';
    command.closest('label').hidden = select.value!=='command';
    for (const name of ['compose_project','compose_files','unit']) {
      const input = form.querySelector(`[name=${name}]`);
      // New projects have no systemd field, and their Compose fields may stay empty.
      if (input) input.required = !('optional' in input.dataset) && (name==='unit' ? select.value==='systemd' : select.value==='compose');
    }
  }
  select.onchange=toggle;
  toggle();
}

function readRuntime(form) {
  const f=Object.fromEntries(new FormData(form));
  return f.runtime==='compose' ? {project:f.compose_project,files:f.compose_files.split(',').map(x=>x.trim()).filter(Boolean)} :
    f.runtime==='systemd' ? {unit:f.unit} : {};
}

async function discoveryPage(t) {
  const accounts=await api('/api/accounts');
  if(t!==epoch)return;
  $('#content').innerHTML=header('已有项目','读取这台服务器上正在运行的服务和它们的仓库。')+`
    <form id="scan-form" class="scan-form"><label>扫描其他目录<input id="scan-root" required placeholder="例如 /opt 或 /srv/apps"></label><button class="primary">扫描目录</button></form>
    <p id="error" class="error" role="alert"></p><div id="scan-results"><p class="loading" role="status">正在读取运行中的服务…</p></div>`;
  const KIND = {git:'Git 仓库', releases:'版本目录', plain:'普通目录'};
  // Your own repositories, to match apps by name when the directory itself says nothing (e.g. qiyu-diary).
  let repoList = [], rerender = null;
  if(accounts.length) api(`/api/accounts/${accounts[0].id}/repositories`).then(rs=>{ repoList = rs; if(t===epoch && rerender) rerender() }).catch(()=>{});
  function repoText(p){
    if(p.repository) return esc(p.repository)+(p.branch?' · '+esc(p.branch):'')+(p.repository_from==='image'?' <span class="muted">（镜像标注的源码仓库）</span>':'');
    const m = matchRepo(p, repoList);
    if(m) return `${esc(m.full_name)} · ${esc(m.default_branch)} <span class="muted">（按名称匹配${m.private?'，私有仓库':''}）</span>`;
    return '未关联 GitHub 仓库';
  }
  function show(result,title,emptyText) {
    function render() {
      rerender = ()=>{ if(document.querySelector('.discovery-row')) render() };
      $('#scan-results').innerHTML=result.warnings.map(w=>`<p class="notice">${esc(w)}</p>`).join('')+
        `<section class="section"><h2>${esc(title(result.projects.length))}</h2><div class="sheet">${result.projects.map((p,i)=>`
          <div class="discovery-row" style="--i:${i}"><div><strong>${esc(p.name)}<span class="kind">${KIND[p.kind]||'目录'}</span></strong><small class="repo-path">${esc(p.path)}</small>
            <small>${repoText(p)}${p.dirty?' · 有本地修改':''}</small>${p.note?`<small class="note">${esc(p.note)}</small>`:''}</div>
          <div class="runtime-hint">${p.containers.length?`Compose · ${p.containers.length} 个容器`:p.services.length?esc(p.services.map(s=>s.unit).join('、')):'没有找到运行中的服务'}</div>
          <button class="pick-project" data-index="${i}" ${p.imported?'disabled':''}>${p.imported?'已接入':'接入'}</button></div>`).join('')||`<p class="empty">${esc(emptyText)}</p>`}</div></section>
        <details class="container-list"><summary>Docker 容器 · ${result.containers.length}</summary>${result.containers.map(c=>`<p><strong>${esc(c.name)}</strong> · ${esc(c.status)}${c.project?` · Compose 项目 ${esc(c.project)}`:''}<br><small>${esc(c.path||'不是用 Compose 启动的，没有项目目录')}</small></p>`).join('')||'<p class="muted">没有可读取的容器。</p>'}</details>`;
      document.querySelectorAll('.pick-project').forEach(b=>b.onclick=()=>importForm(result.projects[Number(b.dataset.index)],accounts,render));
    }
    render();
  }
  let manual=false;
  $('#scan-form').onsubmit=e=>{
    e.preventDefault();
    manual=true;
    busy(e.submitter,async()=>{
      const result=await api('/api/discovery/scan',{root:$('#scan-root').value,depth:3});
      if(t!==epoch)return;
      show(result,n=>`找到 ${n} 个应用`,'这个目录下没有找到应用：没有 Git 仓库、Compose 文件，也没有服务从这里运行。');
    });
  };
  // No directory needed: start from the Compose projects and systemd services that are running.
  try {
    const result=await api('/api/discovery/running');
    if(t!==epoch||manual)return;
    show(result,n=>`正在运行的应用 · ${n}`,'没有找到从应用目录运行的 Compose 项目或 systemd 服务，可以在上面扫描目录。');
  } catch(e) {
    if(t===epoch&&!manual){$('#scan-results').innerHTML='';inlineError(e)}
  }
}

// Same name as the app folder, its Compose project or its systemd unit.
function guessesFor(p){
  const container=p.containers.find(c=>c.project), service=p.services[0];
  return [p.name,container?.project,service?.unit?.replace(/\.service$/,'')].filter(Boolean).map(s=>s.toLowerCase());
}
function matchRepo(p,repos){
  const guesses=guessesFor(p);
  return repos.find(r=>r.full_name===p.repository)||repos.find(r=>guesses.includes(r.full_name.split('/')[1].toLowerCase()));
}

function importForm(p,accounts,back) {
  const container=p.containers.find(c=>c.project&&c.files.length&&c.files[0]);
  const service=p.services[0];
  const git=p.kind==='git';
  // A release layout usually has its own updater: let it do the update, as a command.
  const runtime=p.kind==='releases'?'command':container?'compose':service?'systemd':'command';
  const settings={project:container?.project,files:container?.files||p.compose_files,unit:service?.unit};
  $('#scan-results').innerHTML=`<section class="section"><button class="text" id="back-results">← 扫描结果</button><h2>接入 ${esc(p.name)}</h2><p class="muted repo-path">${esc(p.path)}</p>
    ${p.dirty?'<p class="notice">可以先接入查看。更新前需要处理本地修改。</p>':''}
    ${git?'':`<p class="notice">${esc(p.note||'不是 Git 仓库。')} 更新时平台不拉代码、不改文件，只执行下面的构建命令和重启方式，然后做健康检查；不能回滚。</p>`}
    ${accounts.length?`<form id="import-form" class="form"><label>GitHub 账号<select name="account_id">${accounts.map(a=>`<option value="${a.id}">${esc(a.login)}</option>`).join('')}</select></label>
      ${git?'':`<div class="pair"><label>对应的 GitHub 仓库<input name="repository" list="repo-list" required value="${esc(p.repository)}" placeholder="正在读取仓库…" pattern="[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+" autocomplete="off"><datalist id="repo-list"></datalist><small>格式 owner/repo，可以从列表里选。用来检查更新和接收推送；推送后自动执行下面的更新。</small></label><label>分支<input name="branch" required value="${esc(p.branch||'main')}"></label></div>`}
      ${runtimeFields(runtime,settings)}
      <label>${p.kind==='releases'?'更新命令':'重启命令'}<input name="start_command" placeholder="${p.kind==='releases'?'例如 systemctl start qiyu-updater':'例如 pm2 restart my-app --update-env'}">${p.kind==='releases'?'<small>填这个项目自己的更新命令，要等更新完成再退出。</small>':''}</label>
      <label>构建命令<input name="build_command" placeholder="可留空；Compose 会自动构建"></label>
      <label>健康检查<input name="healthcheck_url" type="url" placeholder="http://127.0.0.1:3000/health"></label>
      <label>健康检查最长等待（秒）<input name="healthcheck_timeout" type="number" required min="5" max="600" value="45"></label>
      <p class="settings-note">接入只登记配置。点击项目里的更新按钮后，才会更新代码并重启服务。</p>
      <div class="buttons"><button class="primary">确认接入</button><button type="button" id="cancel-import">取消</button></div></form>`:'<a class="button" href="#/github">先连接 GitHub</a>'}</section>`;
  $('#back-results').onclick=back;
  if(!accounts.length)return;
  const form=$('#import-form');
  bindRuntime(form);
  $('#cancel-import').onclick=back;
  // A text field with suggestions: still usable when the repository list cannot be read.
  async function repos(){
    const input=form.querySelector('[name=repository]');
    if(!input)return;
    try{
      const rs=await api(`/api/accounts/${form.account_id.value}/repositories`);
      if(!input.isConnected)return;
      $('#repo-list').innerHTML=rs.map(r=>`<option value="${esc(r.full_name)}">${r.private?'私有':'公开'} · 默认分支 ${esc(r.default_branch)}</option>`).join('');
      input.placeholder='owner/repo';
      // Prefill the repository found in the directory, else one whose name matches the app.
      const pick=matchRepo(p,rs);
      if(!input.value&&pick)input.value=pick.full_name;
      if(pick&&!p.branch)form.branch.value=pick.default_branch;
      input.onchange=()=>{const r=rs.find(r=>r.full_name===input.value.trim());if(r)form.branch.value=r.default_branch};
    }catch(e){ if(input.isConnected){ input.placeholder='读不到仓库列表，请直接输入 owner/repo'; inlineError(e) } }
  }
  form.account_id.onchange=repos;
  repos();
  form.onsubmit=e=>{
    e.preventDefault();
    busy(e.submitter,async()=>{
      const f=Object.fromEntries(new FormData(form));
      const result=await api('/api/projects/import',{path:p.path,account_id:Number(f.account_id),repository:f.repository||'',branch:f.branch||'',runtime:f.runtime,runtime_config:readRuntime(form),start_command:f.start_command,build_command:f.build_command,healthcheck_url:f.healthcheck_url,healthcheck_timeout:Number(f.healthcheck_timeout)});
      toast('已接入，现有服务未改动');location.hash=`/project/${result.id}`;
    });
  };
}
