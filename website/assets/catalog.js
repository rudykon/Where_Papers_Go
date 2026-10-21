/** Identity lookup UI. Ranking and filtering are performed in the shared worker. */
export function createCatalogUI({ locale, send, retry, openDetail, exportRows }) {
  const $ = (id) => document.getElementById(id);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const t = (zh, en) => locale() === 'en' ? en : zh;
  const number = (value) => Number(value).toLocaleString(locale() === 'en' ? 'en-US' : 'zh-CN');
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const normalize = (value) => String(value ?? '').normalize('NFKC').toLowerCase().trim().replace(/\s+/g, ' ');
  const rankPrefix = { ccf:'CCF-', th_cpl:'THCPL-', cas:'CAS-', jcr:'JCR-' };
  const state = { source:'all', targets:[], recordType:'all', sortBy:'name', sortDirection:'asc' };
  let ready = false, active = false, info = null, sequence = 0, pending = '', exporting = '', timer;
  let rows = [], result = null, completed = null, requested = null, error = '', busy = false, composing = false;
  const id = () => `lookup-${++sequence}`;
  const params = () => ({ mode:'lookup', ...state, targets:[...state.targets], query:$('lookup-query').value.trim(),
    searchField:$('lookup-field').value, matchMode:$('lookup-match').value, area:$('lookup-area').value,
    reviewedOnly:$('lookup-reviewed').checked, limit:Number($('lookup-page-size').value), offset:0 });

  function renderRanks() {
    if (!info) return;
    const ranks = info.targets.filter((rank) => state.source === 'all' || rank.startsWith(rankPrefix[state.source]));
    const order = ['CCF-A','CCF-B','CCF-C','THCPL-A','THCPL-B','CAS-1','CAS-2','CAS-3','CAS-4','JCR-Q1','JCR-Q2','JCR-Q3','JCR-Q4','JCR-N/A'];
    ranks.sort((a,b) => (order.indexOf(a)<0 ? 100 : order.indexOf(a))-(order.indexOf(b)<0 ? 100 : order.indexOf(b)) || a.localeCompare(b));
    $('lookup-ranks').innerHTML = `<button type="button" data-rank="all" aria-pressed="${!state.targets.length}" class="${state.targets.length ? '' : 'selected'}">${escape(t('全部','All'))}</button>`
      + ranks.map((rank) => `<button type="button" data-rank="${escape(rank)}" aria-pressed="${state.targets.includes(rank)}" class="${state.targets.includes(rank) ? 'selected' : ''}">${escape(rank.replace('THCPL','TH-CPL'))}</button>`).join('');
  }
  function renderControls() {
    renderRanks();
    all('#lookup-sources button').forEach((button) => { const selected=button.dataset.source===state.source;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected)); });
    all('#lookup-types button').forEach((button) => { const selected=button.dataset.recordType===state.recordType;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected)); });
    all('[data-sort-column]').forEach((cell) => {
      const selected=cell.dataset.sortColumn===state.sortBy;
      cell.setAttribute('aria-sort',selected ? (state.sortDirection==='asc' ? 'ascending':'descending') : 'none');
      cell.querySelector('.sort-indicator').textContent=selected ? (state.sortDirection==='asc' ? '↑':'↓') : '↕';
    });
    $('lookup-clear').setAttribute('aria-label',t('清空搜索','Clear search'));
    $('lookup-query').placeholder = $('lookup-field').value === 'issn'
      ? t('输入 ISSN 或 eISSN，例如 0162-8828…','Enter ISSN or eISSN, e.g. 0162-8828…')
      : t('输入简称、全称、出版社或 ISSN，例如 CVPR、IEEE…','Enter abbreviation, full name, publisher or ISSN, e.g. CVPR, IEEE…');
    $('lookup-area').options[0].textContent=t('全部研究分类','All research areas');
  }
  function status() {
    if (error) { $('lookup-status').textContent=t('查找未完成，请重试。','Lookup failed. Please retry.');return; }
    if (!ready || busy) {
      $('lookup-status').textContent=ready ? t('正在筛选…','Filtering…') : t('正在准备本地目录…','Preparing the local catalog…');
      return;
    }
    if (result) {
      const first=result.total ? result.offset+1 : 0, last=result.offset+rows.length;
      $('lookup-status').textContent=t(`找到 ${number(result.total)} 条 · 显示 ${number(first)}–${number(last)} · 按字段查找`,`${number(result.total)} venues · ${number(first)}–${number(last)} shown · Field lookup`);
    }
  }
  function availability() {
    $('lookup-export').disabled=!ready || busy || Boolean(error) || Boolean(exporting) || !result?.total;
    $('lookup-previous').disabled=busy || !result || result.offset===0;
    $('lookup-next').disabled=busy || !result || result.offset+rows.length>=result.total;
    $('catalog-results').setAttribute('aria-busy',String(!error && (busy || !ready)));
    $('lookup-body').classList.toggle('is-loading',busy);
    status();
  }
  function highlight(value, field) {
    const raw=String(value ?? '');
    const query=completed?.query || '';
    if (!query || (completed.searchField!=='all' && completed.searchField!==field)) return escape(raw);
    const normalized=normalize(raw), needle=normalize(query);
    if (normalized===needle) return `<mark>${escape(raw)}</mark>`;
    // Highlight only when normalization preserves character positions.
    if (raw.length===normalized.length && completed.matchMode==='contains') {
      const index=normalized.indexOf(needle);
      if(index>=0)return `${escape(raw.slice(0,index))}<mark>${escape(raw.slice(index,index+needle.length))}</mark>${escape(raw.slice(index+needle.length))}`;
    }
    if(field==='issn' && normalized.replace(/[-\s]/g,'')===needle.replace(/[-\s]/g,''))return `<mark>${escape(raw)}</mark>`;
    return escape(raw);
  }
  function sourceLink(venue) {
    try {
      const url=new URL(venue.url || venue.scope_url);
      if(['http:','https:'].includes(url.protocol) && !url.username && !url.password)
        return `<a href="${escape(url.href)}" target="_blank" rel="noopener noreferrer">${escape(t('来源','Source'))} ↗</a>`;
    } catch { /* Missing source links remain empty. */ }
    return '<span class="lookup-missing">—</span>';
  }
  function render() {
    if (error) {
      ['lookup-total','lookup-conferences','lookup-journals','lookup-reviewed-count'].forEach((name)=>{ $(name).textContent='—'; });
      $('lookup-body').innerHTML=`<tr><td colspan="8" class="lookup-message"><p>${escape(error)}</p><button class="button small" data-lookup-retry type="button">${escape(t('重新加载','Retry loading'))} ↻</button></td></tr>`;
      $('lookup-pagination').hidden=true;
    } else if (result) {
      $('lookup-total').textContent=number(result.total);
      $('lookup-conferences').textContent=number(result.summary?.conference || 0);
      $('lookup-journals').textContent=number(result.summary?.journal || 0);
      $('lookup-reviewed-count').textContent=number(result.summary?.reviewed || 0);
      $('lookup-body').innerHTML=rows.length ? rows.map((venue,index)=>`<tr><td class="lookup-row-number">${number(result.offset+index+1)}</td><td class="lookup-abbreviation">${venue.abbreviation ? highlight(venue.abbreviation,'abbreviation') : '<span class="lookup-missing">—</span>'}</td><td class="lookup-name"><button type="button" data-lookup-detail="${index}" title="${escape(t('查看刊会详情','View venue details'))}">${highlight(venue.name,'name')}</button>${venue.issns?.length ? `<small>${venue.issns.map((issn)=>highlight(issn,'issn')).join(' · ')}</small>` : ''}${venue.reviewed ? `<span class="lookup-reviewed-badge">✓ ${escape(t('范围已审核','Reviewed scope'))}</span>` : ''}</td><td class="lookup-rank-cell">${venue.ranks.map((rank)=>`<span class="lookup-rank-badge">${escape(rank.replace('THCPL','TH-CPL'))}</span>`).join('')}</td><td class="lookup-area-cell">${escape(venue.areas.join(' · ') || '—')}</td><td class="lookup-type-cell">${escape(venue.record_type==='conference' ? t('会议','Conference') : t('期刊','Journal'))}</td><td class="lookup-publisher">${venue.publishers?.length ? venue.publishers.map((publisher)=>highlight(publisher,'publisher')).join('<br>') : '<span class="lookup-missing">—</span>'}</td><td class="lookup-links">${sourceLink(venue)}<button type="button" data-lookup-detail="${index}">${escape(t('详情','Details'))}</button></td></tr>`).join('')
        : `<tr><td colspan="8" class="lookup-message"><strong>${escape(t('没有符合条件的刊会','No matching venues'))}</strong><p>${escape(t('请检查名称或编号，尝试“包含文字”或放宽筛选。此入口不扩展相关主题。','Check the name or identifier, try “Contains text”, or broaden the filters. This lookup does not expand related topics.'))}</p><button class="button small" type="button" data-lookup-reset>${escape(t('重置全部筛选','Reset all filters'))} ↺</button></td></tr>`;
      $('lookup-pagination').hidden=result.total<=result.limit;
      $('lookup-page-label').textContent=`${Math.floor(result.offset/result.limit)+1} / ${Math.max(1,Math.ceil(result.total/result.limit))}`;
    } else {
      $('lookup-body').innerHTML=`<tr><td colspan="8" class="lookup-message">${escape(t('正在准备本地目录…','Preparing the local catalog…'))}</td></tr>`;
    }
    availability();
  }
  function run(offset=0, snapshot=null) {
    clearTimeout(timer);
    if (!active) return;
    if (!ready && error) { retry();return; }
    error='';busy=true;pending=id();requested={...(snapshot || params()),offset};
    availability();
    if(!ready)return;
    if(!send({type:'search',id:pending,...requested})) { ready=false;availability(); }
  }
  function changed(debounce=false) {
    clearTimeout(timer);pending='';renderControls();
    // Editing a failed lookup must leave the retry action available.
    if (!ready && error) { busy=false;render();return; }
    error='';busy=true;availability();
    if (composing || !active)return;
    if(debounce)timer=setTimeout(()=>run(),160);else run();
  }
  function reset() {
    Object.assign(state,{source:'all',targets:[],recordType:'all',sortBy:'name',sortDirection:'asc'});
    $('lookup-query').value='';$('lookup-field').value='all';$('lookup-match').value='contains';$('lookup-area').value='';$('lookup-reviewed').checked=false;
    changed();
  }
  $('lookup-form').addEventListener('submit',(event)=>{event.preventDefault();if(!composing)run();});
  $('lookup-query').addEventListener('compositionstart',()=>{composing=true;clearTimeout(timer);pending='';busy=!(error && !ready);availability();});
  $('lookup-query').addEventListener('compositionend',()=>{composing=false;changed(true);});
  $('lookup-query').addEventListener('input',(event)=>{if(!event.isComposing && !composing)changed(true);});
  $('lookup-clear').addEventListener('click',()=>{$('lookup-query').value='';changed();$('lookup-query').focus();});
  ['lookup-field','lookup-match','lookup-area','lookup-reviewed','lookup-page-size'].forEach((name)=>$(name).addEventListener('change',()=>changed()));
  $('lookup-reset').addEventListener('click',reset);
  $('lookup-sources').addEventListener('click',(event)=>{const button=event.target.closest('[data-source]');if(!button)return;state.source=button.dataset.source;state.targets=state.targets.filter((rank)=>state.source==='all' || rank.startsWith(rankPrefix[state.source]));changed();});
  $('lookup-types').addEventListener('click',(event)=>{const button=event.target.closest('[data-record-type]');if(button){state.recordType=button.dataset.recordType;changed();}});
  $('lookup-ranks').addEventListener('click',(event)=>{const button=event.target.closest('[data-rank]');if(!button)return;const rank=button.dataset.rank;state.targets=rank==='all' ? [] : state.targets.includes(rank) ? state.targets.filter((value)=>value!==rank) : [...state.targets,rank];changed();});
  all('[data-lookup-sort]').forEach((button)=>button.addEventListener('click',()=>{const column=button.dataset.lookupSort;state.sortDirection=state.sortBy===column && state.sortDirection==='asc' ? 'desc':'asc';state.sortBy=column;changed();}));
  $('lookup-previous').addEventListener('click',()=>{if(!busy && completed){run(Math.max(0,result.offset-result.limit),completed);$('catalog-results').scrollIntoView({behavior:'instant'});}});
  $('lookup-next').addEventListener('click',()=>{if(!busy && completed){run(result.offset+result.limit,completed);$('catalog-results').scrollIntoView({behavior:'instant'});}});
  $('lookup-export').addEventListener('click',()=>{if(!completed || busy || exporting || !ready)return;exporting=id();send({type:'export',id:exporting,...completed});availability();});
  $('lookup-body').addEventListener('click',(event)=>{const detail=event.target.closest('[data-lookup-detail]');if(detail && !busy){const venue=rows[Number(detail.dataset.lookupDetail)];if(venue)openDetail(venue);}if(event.target.closest('[data-lookup-reset]'))reset();if(event.target.closest('[data-lookup-retry]')){error='';ready=false;result=null;busy=true;render();retry();}});

  return {
    activate(value) { active=value;clearTimeout(timer);if(!value){pending='';return;}renderControls();if(error){render();return;}if(ready && (!result || busy))run();else render(); },
    ready(data) { info=data;ready=true;error='';const area=$('lookup-area').value;$('lookup-area').innerHTML=`<option value="">${escape(t('全部研究分类','All research areas'))}</option>`+data.areas.map((value)=>`<option value="${escape(value)}">${escape(value)}</option>`).join('');$('lookup-area').value=area;renderControls();if(active)run(); },
    loading() { ready=false;pending='';exporting='';error='';busy=true;result=null;rows=[];render(); },
    failure(message) { ready=false;pending='';exporting='';error=message;busy=false;render(); },
    translate() { renderControls();render(); },
    focus() { $('lookup-query').focus(); },
    handle(data) {
      if(data.id===exporting && exporting){if(data.type==='export'){exporting='';exportRows(data.results);availability();}else if(data.type==='error'){exporting='';$('lookup-status').textContent=data.message;$('lookup-export').disabled=false;}return true;}
      if(data.id!==pending || !pending)return false;
      if(data.type==='results'){pending='';busy=false;result=data;rows=data.results;completed=requested;render();}
      else if(data.type==='error'){pending='';busy=false;error=data.message;render();}
      return true;
    },
  };
}
