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
      form.querySelector(`[name=${name}]`).required = name==='unit' ? select.value==='systemd' : select.value==='compose';
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
  $('#content').innerHTML=header('已有项目','读取这台服务器上的仓库和服务。')+`
    <form id="scan-form" class="scan-form"><label>项目所在目录<input id="scan-root" required placeholder="例如 /opt 或 /srv/apps"></label><button class="primary">扫描目录</button></form>
    <p id="error" class="error" role="alert"></p><div id="scan-results"></div>`;
  $('#scan-form').onsubmit=e=>{
    e.preventDefault();
    busy(e.submitter,async()=>{
      e.submitter.textContent='正在扫描…';
      try {
        const result=await api('/api/discovery/scan',{root:$('#scan-root').value,depth:3});
        if(t!==epoch)return;
        $('#error').textContent='';
        function render() {
          $('#scan-results').innerHTML=result.warnings.map(w=>`<p class="notice">${esc(w)}</p>`).join('')+
            `<section class="section"><h2>找到 ${result.projects.length} 个仓库</h2><div class="sheet">${result.projects.map((p,i)=>`
              <div class="discovery-row"><div><strong>${esc(p.name)}</strong><small class="repo-path">${esc(p.path)}</small><small>${esc(p.repository||'非 GitHub 仓库')} · ${esc(p.branch||'未检出分支')}${p.dirty?' · 有本地修改':''}</small></div>
              <div class="runtime-hint">${p.containers.length?`Compose · ${p.containers.length} 个容器`:p.services.length?esc(p.services.map(s=>s.unit).join('、')):'待设置重启方式'}</div>
              <button class="pick-project" data-index="${i}" ${!p.repository||!p.branch?'disabled':''}>接入</button></div>`).join('')||'<p class="empty">这个目录下没有找到 Git 仓库。</p>'}</div></section>
            <details class="container-list"><summary>Docker 容器 · ${result.containers.length}</summary>${result.containers.map(c=>`<p><strong>${esc(c.name)}</strong> · ${esc(c.status)}<br><small>${esc(c.path||'未标注源码目录')}</small></p>`).join('')||'<p class="muted">没有可读取的容器。</p>'}</details>`;
          document.querySelectorAll('.pick-project').forEach(b=>b.onclick=()=>importForm(result.projects[Number(b.dataset.index)],accounts,render));
        }
        render();
      } finally {e.submitter.textContent='扫描目录'}
    });
  };
}

function importForm(p,accounts,back) {
  const container=p.containers.find(c=>c.project&&c.files.length&&c.files[0]);
  const service=p.services[0];
  const runtime=container?'compose':service?'systemd':'command';
  const settings={project:container?.project,files:container?.files||p.compose_files,unit:service?.unit};
  $('#scan-results').innerHTML=`<section class="section"><button class="text" id="back-results">← 扫描结果</button><h2>接入 ${esc(p.name)}</h2><p class="muted repo-path">${esc(p.path)}</p>
    ${p.dirty?'<p class="notice">可以先接入查看。更新前需要处理本地修改。</p>':''}
    ${accounts.length?`<form id="import-form" class="form"><label>GitHub 账号<select name="account_id">${accounts.map(a=>`<option value="${a.id}">${esc(a.login)}</option>`).join('')}</select></label>
      ${runtimeFields(runtime,settings)}
      <label>重启命令<input name="start_command" placeholder="例如 pm2 restart my-app --update-env"></label>
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
  form.onsubmit=e=>{
    e.preventDefault();
    busy(e.submitter,async()=>{
      const f=Object.fromEntries(new FormData(form));
      const result=await api('/api/projects/import',{path:p.path,account_id:Number(f.account_id),runtime:f.runtime,runtime_config:readRuntime(form),start_command:f.start_command,build_command:f.build_command,healthcheck_url:f.healthcheck_url,healthcheck_timeout:Number(f.healthcheck_timeout)});
      toast('已接入，现有服务未改动');location.hash=`/project/${result.id}`;
    });
  };
}
