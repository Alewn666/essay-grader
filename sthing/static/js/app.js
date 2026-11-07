/* 作文批改 —— 前端 */
'use strict';

var $ = function (s) { return document.querySelector(s); };
var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

var state = { id: null, doc: null, images: [], promptFile: null, promptData: null, category: '' };

/* ---------------- 401 自动回登录页（session 过期时） ---------------- */
(function () {
  var reloaded = false;
  // 遮罩存在 = 服务端判定「没登录」，这一页本身就是登录页
  // （setupOverlay = 还没设口令的锁定页，同样不能 reload —— 它的 API 全 403）
  var onLoginPage = function () {
    return !!document.getElementById('loginOverlay') ||
           !!document.getElementById('setupOverlay');
  };
  var origFetch = window.fetch;
  window.fetch = function () {
    var url = arguments[0];
    var u = url ? url.toString() : '';
    // /api/login 自己处理 401 的 UI，别碰
    if (u.indexOf('/api/login') === -1) {
      // 不是登录请求 → 检查 401 就 reload（后端会渲染登录遮罩）
      return origFetch.apply(this, arguments).then(function (r) {
        if (r.status === 401) {
          // ⚠️ 已经在登录页时**绝不能**再 reload：页面照样执行 loadList() →
          //    401 → reload → 又 loadList() → 又 401…… 服务端日志里
          //    `GET / → GET /api/essays 401 → GET /` 的死循环就是这么来的
          //    （第三十二轮审查 #3）。reload 最多也只做一次。
          if (onLoginPage() || reloaded) return r;
          reloaded = true;
          location.reload();
          return r;
        }
        return r;
      });
    }
    return origFetch.apply(this, arguments);
  };
  window.__onLoginPage = onLoginPage;
})();

/* ---------------- 页面内登录（设了访问口令时才渲染遮罩） ---------------- */
(function () {
  var ov = document.getElementById('loginOverlay');
  if (!ov) return;                    // 没配口令 → 遮罩不存在 → 正常用
  var pw = document.getElementById('loginPw');
  var err = document.getElementById('loginErr');
  var btn = document.getElementById('btnLogin');
  function doLogin() {
    var v = pw.value;
    if (!v) { pw.focus(); return; }
    btn.disabled = true; btn.textContent = '验证中…';
    fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: v })
    }).then(function (r) {
      if (r.ok) {
        location.reload();             // 直接 reload，后端重新渲染时不带遮罩，不会闪
      } else {
        return r.json().then(function (d) {
          err.textContent = d.error || '口令不正确';
          err.classList.remove('hidden');
          pw.value = ''; pw.focus();
        });
      }
    }).catch(function () {
      err.textContent = '网络错误，重试';
      err.classList.remove('hidden');
    }).finally(function () {
      btn.disabled = false; btn.textContent = '进入';
    });
  }
  btn.addEventListener('click', doLogin);
  pw.addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
  pw.focus();
})();

/* ---------------- 视图切换 ---------------- */
var curView = '';
function show(view) {
  // 动态遍历所有 .view：控制面板视图只在开了 control_panel 的页面里存在
  var views = document.querySelectorAll('main .view');
  for (var i = 0; i < views.length; i++) {
    views[i].classList.toggle('hidden', views[i].id !== 'view-' + view);
  }
  // 详情页在宽屏下走三栏排版（main 放宽），其它页面维持 860px 窄栏
  document.body.classList.toggle('detail-open', view === 'detail');
  // ⚠️ 只有**换页**才回顶部。同页刷新（AI 答完 / 自愈轮询拉数据）保留阅读位置 ——
  //    以前每次 openDetail 都 scrollTo(0,0)，回答一完整页拍回顶上，得重新往下翻。
  if (view !== curView) window.scrollTo(0, 0);
  curView = view;
}

function toast(msg, ms) {
  var t = $('#toast');
  // ⚠️ 用 textContent 不用 innerHTML：msg 里可能拼着模型返回/异常串
  //    （如 toast('批改失败：' + d.error)），HTML 会被当真标签解析 —— 第十六轮审查 #5。
  //    全部 20 个调用点传的都是纯文本，没有一处需要 HTML。
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._timer);
  t._timer = setTimeout(function () { t.classList.add('hidden'); }, ms || 2200);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtDate(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  var p = function (n) { return (n < 10 ? '0' : '') + n; };
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

/* ---------------- 列表 ---------------- */
var STATUS_TEXT = { draft: '待批改', pending: '待批改', revising: '批改中', done: '已批改', error: '出错' };

function loadList() {
  fetch('/api/essays').then(function (r) { return r.json(); }).then(function (list) {
    var box = $('#list');
    $('#empty').classList.toggle('hidden', list.length > 0);
    box.innerHTML = list.map(function (it) {
      var st = it.status || 'draft';
      var tagCls = st === 'done' ? 'done' : (st === 'revising' ? 'revising' : (st === 'error' ? 'error' : 'pending'));
      var scoreTxt = it.score != null ? ' · ' + esc(it.score) + '分' : '';
      return '<div class="item" data-id="' + esc(it.id) + '">' +
        '<div class="item-top">' +
          '<div class="item-title">' + esc(it.title || '未命名作文') + '</div>' +
          '<span class="tag ' + tagCls + '">' + (STATUS_TEXT[st] || esc(st)) + '</span>' +
        '</div>' +
        '<div class="item-sub"><span>' + fmtDate(it.created_at) + '</span>' +
          '<span>' + esc(it.chars) + ' 字</span>' +
          (it.pages ? '<span>📄 ' + esc(it.pages) + ' 页</span>' : (it.has_image ? '<span>📄 有原图</span>' : '')) +
          (it.has_docx ? '<span>📝 Word</span>' : '') +
          (it.has_prompt ? '<span>📋 有题目</span>' : '') +
          (it.qa ? '<span>💬 ' + esc(it.qa) + '</span>' : '') +
          '<span>' + scoreTxt + '</span></div>' +
      '</div>';
    }).join('');
    $$('.item').forEach(function (el) {
      el.addEventListener('click', function () { openDetail(el.dataset.id); });
    });
  }).catch(function () { toast('加载失败'); });
}

/* ---------------- 新建 ---------------- */
function bindNew() {
  $('#btnNew').addEventListener('click', resetNew);
  $('#btnBackNew').addEventListener('click', function () { show('list'); loadList(); });
  $('#btnDropAll').addEventListener('click', function () {
    if (!confirm('清空当前内容？')) return;
    resetNew();
    show('list');
    loadList();
  });
  $('#btnCamera').addEventListener('click', function () { $('#fileCamera').click(); });
  $('#btnAlbum').addEventListener('click', function () { $('#fileAlbum').click(); });
  $('#fileCamera').addEventListener('change', onPick);
  $('#fileAlbum').addEventListener('change', onPick);
  $('#btnSave').addEventListener('click', saveNew);
  // 文章类别单选：点哪个是哪个（空 = 让 AI 自己判断）
  var catPick = $('#catPick');
  if (catPick) catPick.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-cat]');
    if (!b) return;
    state.category = b.getAttribute('data-cat') || '';
    catPick.querySelectorAll('button[data-cat]').forEach(function (x) {
      x.classList.toggle('on', x === b);
    });
  });

  /* --- 现代 Word（.docx）：正文直接从文档读，省掉拍照 + 识别 --- */
  $('#btnDocx').addEventListener('click', function () { $('#fileDocx').click(); });
  $('#fileDocx').addEventListener('change', onPickDocx);
  $('#btnDropDocx').addEventListener('click', dropDocx);

  /* --- 考试作文题目（图片让 AI 读，不再本地 OCR）--- */
  $('#btnPromptCam').addEventListener('click', function () { $('#filePromptCam').click(); });
  $('#btnPromptAlbum').addEventListener('click', function () { $('#filePromptAlbum').click(); });
  $('#filePromptCam').addEventListener('change', onPickPrompt);
  $('#filePromptAlbum').addEventListener('change', onPickPrompt);
  $('#btnDropPromptImg').addEventListener('click', dropPromptImg);
  $('#inpPrompt').addEventListener('input', function () {
    $('#promptCount').textContent = $('#inpPrompt').value.length;
  });
}

function resetNew() {
  state.images = [];
  state.promptFile = null; state.promptData = null;
  state.docxFile = null; state.docxData = null;
  state.category = '';
  var catPick = $('#catPick');
  if (catPick) catPick.querySelectorAll('button[data-cat]').forEach(function (x) {
    x.classList.toggle('on', x.getAttribute('data-cat') === '');
  });
  $('#inpTitle').value = '';
  $('#inpPrompt').value = '';
  $('#promptCount').textContent = '0';
  renderPages();
  dropPromptImg();
  dropDocx();
  show('new');
}

/* ---- 作文文档（.docx / 老 .doc / .txt）---- */
var DOCX_MAX = 8 * 1024 * 1024;      // 和后端 docx_text.MAX_BYTES 对齐

function onPickDocx(e) {
  var f = e.target.files && e.target.files[0];
  e.target.value = '';
  if (!f) return;
  var name = f.name || '';
  if (!/\.(docx?|txt)$/i.test(name)) {
    toast('请选作文文档：.doc / .docx / .txt', 5000);
    return;
  }
  if (f.size > DOCX_MAX) {
    toast('文档太大了（上限 8MB）', 4000);
    return;
  }
  state.docxFile = name;
  var reader = new FileReader();
  reader.onload = function () {
    state.docxData = reader.result;        // data:...;base64,xxx，后端自己剥前缀
    $('#docxName').textContent = name + '（' + Math.round(f.size / 1024) + ' KB）';
    $('#docxBox').classList.remove('hidden');
    $('#docxHint').textContent = '已选文档：正文会从文件里读出来，照片可以不传。';
  };
  reader.readAsDataURL(f);
}

function dropDocx() {
  state.docxFile = null; state.docxData = null;
  $('#docxBox').classList.add('hidden');
  $('#docxName').textContent = '';
  $('#docxHint').textContent = '有电子稿最省事：正文直接从文档里读，AI 只做批改。';
}

function onPickPrompt(e) {
  var f = e.target.files && e.target.files[0];
  if (!f) return;
  state.promptFile = f;
  var reader = new FileReader();
  reader.onload = function () {
    state.promptData = reader.result;
    $('#promptPreview').src = state.promptData;
    $('#promptPreviewWrap').classList.remove('hidden');
  };
  reader.readAsDataURL(f);
  e.target.value = '';
}

function dropPromptImg() {
  state.promptFile = null; state.promptData = null;
  $('#promptPreviewWrap').classList.add('hidden');
  $('#promptPreview').removeAttribute('src');
}

/* ---- 作文纸：多张 ---- */
function onPick(e) {
  var files = Array.prototype.slice.call(e.target.files || []);
  if (!files.length) return;
  var left = files.length;
  files.forEach(function (f) {
    var reader = new FileReader();
    reader.onload = function () {
      state.images.push({ name: f.name, data: reader.result });
      if (--left === 0) renderPages();
    };
    reader.readAsDataURL(f);
  });
  e.target.value = '';
}

function renderPages() {
  var box = $('#pageGrid');
  var list = state.images || [];
  box.classList.toggle('hidden', list.length === 0);
  $('#pageHint').classList.toggle('hidden', list.length > 0);
  $('#pageHint').textContent = '还没有照片。拍一张或选几张，AI 会按顺序识别。';

  box.innerHTML = list.map(function (p, i) {
    return '<div class="page-item">' +
      '<img src="' + p.data + '" alt="第' + (i + 1) + '页">' +
      '<div class="page-tag">第 ' + (i + 1) + ' 页</div>' +
      '<button class="page-del" data-i="' + i + '" title="删除">×</button>' +
      '</div>';
  }).join('');

  Array.prototype.forEach.call(box.querySelectorAll('.page-del'), function (b) {
    b.addEventListener('click', function () {
      state.images.splice(parseInt(b.dataset.i, 10), 1);
      renderPages();
    });
  });
}

function compress(dataUrl, maxW, cb) {
  var img = new Image();
  img.onload = function () {
    var scale = Math.min(1, maxW / img.width);
    var c = document.createElement('canvas');
    c.width = Math.round(img.width * scale);
    c.height = Math.round(img.height * scale);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    cb(c.toDataURL('image/jpeg', 0.85));
  };
  img.onerror = function () { cb(dataUrl); };
  img.src = dataUrl;
}

function saveNew() {
  var title = $('#inpTitle').value.trim();
  var prompt = $('#inpPrompt').value.trim();
  var imgs = state.images || [];
  // 作文纸照片和 Word 文档，给一个就行
  if (!imgs.length && !state.docxData) {
    toast('先拍/选作文纸照片，或选一个作文文档（.doc/.docx/.txt）');
    return;
  }

  var btn = $('#btnSave');
  btn.disabled = true; btn.innerHTML = '<span class="loading"></span>保存中…';

  // 每张照片都压到 1400px，按下标存放保证**顺序不乱**
  //（compress 异步回调的返回顺序是不可控的，push 会打乱页序）
  var packed = new Array(imgs.length);
  var left = imgs.length;
  imgs.forEach(function (p, idx) {
    compress(p.data, 1400, function (out) {
      packed[idx] = out;
      if (--left === 0) sendIt();
    });
  });
  if (!imgs.length) sendIt();       // 只传了 Word：没有照片要压，直接发

  function sendIt() {
    var send = function (promptImg) {
      fetch('/api/essays', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: title, prompt: prompt, category: state.category || '',
          images: packed,
          docx: state.docxData || '',
          docx_name: state.docxFile || '',
          prompt_image: promptImg || ''
        })
      }).then(function (r) { return r.json(); }).then(function (res) {
        btn.disabled = false; btn.textContent = '保存';
        if (res.error) { toast(res.error, 5000); return; }
        if (res.from_docx) toast('已保存：从文档读到 ' + (res.chars || 0) + ' 字正文');
        else toast('已保存，共 ' + (res.pages || packed.length) + ' 页');
        loadList();
        openDetail(res.id);
      }).catch(function () {
        btn.disabled = false; btn.textContent = '保存';
        toast('保存失败');
      });
    };
    if (state.promptData) compress(state.promptData, 1200, send);
    else send('');
  }
}

/* ---------------- 详情 ---------------- */
/* ---------------- 标红批注 ----------------
 * 注解按 quote 在正文里**重新定位**，不用后端给的纯偏移：
 * JS 用 UTF-16 码元、Python 用码点，遇到 emoji/生僻字会错位。
 * 两边各用各的语义去 find，天然对齐；找不到就整条作废，绝不猜位置。 */
var annMarks = [];

function resolveAnns(text, anns) {
  var out = [];
  (anns || []).forEach(function (a) {
    var q = a.quote || '';
    if (!q) return;
    var hint = typeof a.start_hint === 'number' ? a.start_hint : 0;
    var idx = text.indexOf(q, Math.max(0, hint - 5));
    if (idx < 0) idx = text.indexOf(q);
    if (idx < 0) return;
    out.push({ s: idx, e: idx + q.length, a: a });
  });
  out.sort(function (x, y) { return x.s - y.s; });
  var clean = [], last = -1;
  out.forEach(function (x) {
    if (x.s >= last) { clean.push(x); last = x.e; }
  });
  return clean;
}

function renderAnnotatedText(text, anns) {
  annMarks = resolveAnns(text || '', anns);
  if (!text) return '<span class="muted">（还没有正文）</span>';
  if (!annMarks.length) return esc(text).replace(/\n/g, '<br>');
  var html = '', pos = 0;
  annMarks.forEach(function (m, i) {
    html += esc(text.slice(pos, m.s));
    html += '<mark class="hl" data-ann="' + i + '">' + esc(text.slice(m.s, m.e)) + '</mark>';
    pos = m.e;
  });
  html += esc(text.slice(pos));
  return html.replace(/\n/g, '<br>');
}

function renderAnnPanel() {
  var panel = $('#annPanel');
  if (!annMarks.length) {
    panel.innerHTML = '';
    panel.classList.add('hidden');
    return;
  }
  panel.innerHTML = annMarks.map(function (m, i) {
    return '<div class="ann-card" id="ann-' + i + '">' +
      '<div class="ann-i">' + (i + 1) + '</div>' +
      '<div class="ann-body">' +
        '<div class="ann-quote">“' + esc(m.a.quote || '') + '”</div>' +
        (m.a.issue ? '<div class="ann-line"><b>问题　</b>' + esc(m.a.issue) + '</div>' : '') +
        (m.a.suggest ? '<div class="ann-line sug"><b>建议　</b>' + esc(m.a.suggest) + '</div>' : '') +
      '</div>' +
    '</div>';
  }).join('');
  panel.classList.remove('hidden');
}

function jumpToAnn(i) {
  var card = $('#ann-' + i);
  if (!card) return;
  $$('#annPanel .ann-card').forEach(function (c) { c.classList.remove('cur'); });
  card.classList.add('cur');
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.remove('flash');
  void card.offsetWidth;              // 强制重排一次，动画才肯重新触发
  card.classList.add('flash');
}

function bindAnn() {
  $('#btnToggleAnn').addEventListener('click', function () {
    var off = $('#dText').classList.toggle('ann-off');
    $('#annPanel').classList.toggle('hidden', off);
    this.classList.toggle('on', !off);
    this.textContent = off ? '🔴 批注' : '🔴 批注中';
  });
  $('#dText').addEventListener('click', function (e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('hl') && !$('#dText').classList.contains('ann-off')) {
      jumpToAnn(parseInt(t.getAttribute('data-ann'), 10));
    }
  });
}

/* 版本历史 / 回滚 已于 2026-09-25 整个移除（用户要求）：
   回滚列表越滚越长，得不偿失；AI 批改只写 result 不动正文，风险可控。 */

function openDetail(id, _retry) {
  state.id = id;
  fetch('/api/essays/' + encodeURIComponent(id)).then(function (r) { return r.json(); }).then(function (doc) {
    // ⚠️ doc.error 有两个含义：接口报错的返回体、或作文自己记录的批改失败原因。
    //    只有「没有 id」才是接口报错，否则批改失败过的作文就永远打不开了。
    if (doc.error && !doc.id) throw new Error(doc.error);
    state.doc = doc;
    renderDetail();
    show('detail');
    settleTries = 0;
    scheduleSettle(id);
  }).catch(function (e) {
    // ⚠️ 拉取失败别把界面晾在半路：实测「取消批改后这一笔 GET 被浏览器卡住」
    //    （服务端 0.46 秒就回了，浏览器侧连接没释放）→ 没人刷新 → 永久卡「批改中」。
    //    这里 2 秒后自动重试，最多 10 次；服务端其实一直是好的。
    if (!_retry) toast((e && e.message) || '加载失败，正在重试…');
    if ((_retry || 0) < 10) {
      setTimeout(function () { openDetail(id, (_retry || 0) + 1); }, 2000);
    }
  });
}

/* 自愈轮询：批改还在服务端跑（status=revising）而这边的流已经断了时，
   定时再拉直到状态落定 —— 否则按钮会永远卡在「批改中」。
   （真机上 SSE 真的中途断过：断后没有任何人再去拉状态，界面就僵死了。） */
var settleTimer = 0, settleTries = 0;
function scheduleSettle(id) {
  clearTimeout(settleTimer);
  var d = state.doc || {};
  if (d.status !== 'revising') return;            // 状态已落定，不用管
  if (state.asking || state.streaming) return;    // 追问/批改进行中别插一杠子
  if (settleTries++ > 60) return;                 // 上限约 2 分钟，别无限轮询
  settleTimer = setTimeout(function () {
    if (state.id !== id) return;
    fetch('/api/essays/' + encodeURIComponent(id))
      .then(function (r) { return r.json(); })
      .then(function (doc) {
        if (!doc || !doc.id) { scheduleSettle(id); return; }   // 请求失效 → 下一轮再试
        state.doc = doc;
        renderDetail();
        scheduleSettle(id);
      })
      .catch(function () { scheduleSettle(id); });
  }, 2000);
}

function renderDetail() {
  var d = state.doc;
  $('#dTitle').textContent = d.title || '未命名作文';
  $('#dMeta').textContent = fmtDate(d.created_at) + ' · ' + (d.text || '').length + ' 字' +
    (d.category ? ' · ' + d.category : '') +
    (d.docx_name ? ' · 📝 ' + d.docx_name : '');

  var imgs = d.images && d.images.length ? d.images
           : (d.image ? [d.image] : []);
  if (imgs.length) {
    $('#imgCard').classList.remove('hidden');
    $('#dImgCount').textContent = '共 ' + imgs.length + ' 页';
    $('#dImages').innerHTML = imgs.map(function (f, i) {
      return '<div class="page-shot"><div class="page-tag">第 ' + (i + 1) + ' 页</div>' +
        '<img src="/api/images/' + encodeURIComponent(f) + '" alt="第' + (i + 1) + '页"></div>';
    }).join('');
  } else {
    $('#imgCard').classList.add('hidden');
  }

  /* 考试作文题目 */
  var pText = (d.prompt || '').trim();
  var pImg = d.prompt_image;
  if (pText || pImg) {
    $('#promptCard').classList.remove('hidden');
    $('#dPrompt').textContent = pText || '（只有题目图，没识别文字）';
    $('#dPrompt').classList.toggle('hidden', !pText);
    if (pImg) {
      $('#dPromptImage').src = '/api/images/' + pImg;
      $('#dPromptImage').classList.remove('hidden');
    } else {
      $('#dPromptImage').classList.add('hidden');
      $('#dPromptImage').removeAttribute('src');
    }
  } else {
    $('#promptCard').classList.add('hidden');
  }

  var hasText = !!(d.text && d.text.trim());
  $('#dText').innerHTML = hasText
    ? renderAnnotatedText(d.text, d.annotations)
    : '<span class="muted">（还没有识别正文 —— 点下面「让 AI 老师批改」，AI 会直接从照片里抄出来）</span>';
  $('#dText').classList.remove('hidden', 'ann-off');
  $('#dEditText').classList.add('hidden');
  $('#dEditText').value = d.text || '';
  $('#editActions').classList.add('hidden');
  renderAnnPanel();

  var n = annMarks.length;
  $('#btnToggleAnn').textContent = n ? ('🔴 批注 (' + n + ')') : '🔴 批注';
  $('#btnToggleAnn').classList.toggle('on', n > 0);
  $('#btnToggleAnn').disabled = !n;

  renderResult(d.result, d.status, d.error);
  renderQa();
}

function renderResult(result, status, error) {
  var area = $('#resultArea');        /* 批改意见（逐段批注/提分建议）—— 桌面右栏 */
  var scoreArea = $('#scoreArea');    /* 成绩区（分数/扣题/四维度/扣分/字数）—— 桌面左栏 */
  var revArea = $('#revisedArea');
  var btn = $('#btnRevise');
  var hint = $('#reviseHint');

  /* 流式批改进行中：按钮和提示条归 doRevise 管（那里有「取消」），
     这里只负责往结果区里长内容，别把它们冲掉。 */
  if (state.streaming) {
    btn.disabled = true;
    btn.textContent = 'AI 老师批改中…';
  } else if (status === 'revising') {
    btn.disabled = true;
    btn.innerHTML = '<span class="loading"></span>AI 老师批改中…';
    hint.textContent = '一篇作文大约需要 20~60 秒';
  } else {
    btn.disabled = false;
    btn.textContent = result ? '🔄 重新批改' : '✨ 让 AI 老师批改';
    hint.textContent = '';
  }

  if (status === 'pending') {
    hint.textContent = '还没配置 AI 接口，已存为「待批改」';
  }
  if (error) {
    hint.innerHTML = '<span style="color:var(--danger)">' + esc(error) + '</span>';
  }

  if (!result) { area.innerHTML = ''; scoreArea.innerHTML = ''; revArea.innerHTML = ''; return; }

  /* 四维度：等第徽章（这套标准是「整体定档」，不是逐项打分求和） */
  var dims = (result.dims || []).map(function (d) {
    var lv = d.level || '';
    var cls = lv.indexOf('一') === 0 ? 'l1' : lv.indexOf('二') === 0 ? 'l2'
      : lv.indexOf('三') === 0 ? 'l3' : lv.indexOf('四') === 0 ? 'l4' : 'l5';
    return '<div class="dim-row2">' +
        '<div class="dim-name2">' + esc(d.name) + '</div>' +
        '<span class="lvl ' + cls + '">' + esc(lv || '—') + '</span>' +
      '</div>' +
      (d.comment ? '<div class="dim-comment">' + esc(d.comment) + '</div>' : '');
  }).join('');

  /* 扣分明细 */
  var deds = (result.deductions || []).map(function (x) {
    return '<div class="ded-row"><span class="ded-rule">' + esc(x.rule) + '</span>' +
      '<span class="ded-pts">−' + esc(x.points != null ? x.points : 0) + ' 分</span></div>' +
      (x.note ? '<div class="dim-comment">' + esc(x.note) + '</div>' : '');
  }).join('');
  var dedBox = '';
  if (deds) {
    dedBox = '<div class="card"><div class="lbl">扣分明细</div>' +
      '<div class="ded-flow"><b>' + esc(result.base_score != null ? result.base_score : '--') + '</b> 档次分' +
      ' <span class="arrow">−</span> <b>' + esc(deds2total(result.deductions)) + '</b> 扣分' +
      ' <span class="arrow">=</span> <b class="final">' + esc(result.score != null ? result.score : '--') + '</b> 最终分</div>' +
      deds + '</div>';
  }

  var paras = (result.comments || []).map(function (c) {
    return '<div class="para-block">' +
      '<div class="para-quote">第 ' + esc(c.para) + ' 段' + (c.quote ? '　“' + esc(c.quote) + '”' : '') + '</div>' +
      (c.good ? '<div class="note good"><b>【好】</b>' + esc(c.good) + '</div>' : '') +
      (c.improve ? '<div class="note improve"><b>【改】</b>' + esc(c.improve) + '</div>' : '') +
      (c.risk ? '<div class="note risk"><b>【疑】</b>' + esc(c.risk) + '</div>' : '') +
    '</div>';
  }).join('');

  var sugs = (result.suggestions || []).map(function (s, i) {
    return '<div class="sug"><div class="sug-i">' + (i + 1) + '</div><div>' + esc(s) + '</div></div>';
  }).join('');

  /* 成绩区 → 左栏 #scoreArea（分数/扣题/四维度/扣分明细/字数） */
  scoreArea.innerHTML =
    '<div class="score-hero">' +
      '<div class="score-num">' + esc(result.score != null ? result.score : '--') + '<small>/60</small></div>' +
      '<div class="score-meta">' +
        '<div class="score-level">' + esc(result.level || '') +
          (result.level_range ? ' <span class="range">' + esc(result.level_range) + '</span>' : '') +
        '</div>' +
        (result.band_reason ? '<div class="score-sum">📌 ' + esc(result.band_reason) + '</div>' : '') +
        '<div class="score-sum">' + esc(result.summary || '') + '</div>' +
      '</div>' +
    '</div>' +
    (result.on_topic ? '<div class="card"><div class="lbl">是否扣题</div>' +
      '<div class="on-topic">' + esc(result.on_topic) + '</div></div>' : '') +
    (dims ? '<div class="card"><div class="lbl">四维度等第 <span class="muted small">（情境 · 内容 · 结构 · 语言）</span></div>' + dims + '</div>' : '') +
    dedBox +
    (result.word_count ? '<div class="card"><div class="lbl">字数</div><div class="on-topic">正文 <b>' +
      esc(result.word_count) + '</b> 字' + (result.word_count < 500 ? '　<span class="warn">不足 500 字，已按规则扣分</span>' : '') +
      '</div></div>' : '');

  /* 批改意见（逐段批注/提分建议）→ 右栏 #resultArea */
  area.innerHTML =
    (paras ? '<div class="card"><div class="lbl">逐段批注</div>' + paras + '</div>' : '') +
    (sugs ? '<div class="card"><div class="lbl">提分建议</div>' + sugs + '</div>' : '');

  /* 修改后全文 + 下载按钮 → 中栏 #revisedArea（跟在原文后面，手机上排在批改意见之后） */
  revArea.innerHTML =
    (result.revised ? '<div class="card"><div class="card-head"><span class="lbl">✍️ 修改后的文章</span>' +
      '<button class="btn tiny ghost" id="btnCopy">复制</button></div>' +
      '<div class="revised">' + esc(result.revised) + '</div></div>' : '') +
    /* ⬇️ 一键下载批改报告（TXT）：走 <a href> 原生下载，手机上直接进下载/分享 */
    ((result && !state.streaming && status !== 'revising')
      ? '<div class="card"><a class="btn primary wide" id="btnReport" ' +
        'href="/api/essays/' + encodeURIComponent(state.id) + '/report.txt" download ' +
        'style="text-decoration:none;text-align:center">⬇️ 下载批改报告（TXT）</a>' +
        '<div class="muted small center">基本情况 + 详细修改意见 + 修改后全文</div></div>'
      : '');

  var cp = $('#btnCopy');
  if (cp) cp.addEventListener('click', function () {
    navigator.clipboard.writeText(result.revised).then(function () { toast('已复制'); },
      function () { toast('复制失败'); });
  });

  balanceCols();
  balanceSoon();   /* 照片 / 追问列表渲染完高度才准，稍后再摆一次 */
}

/* 左右栏平衡 —— 内容体量按篇差别极大（实测生产 6 篇：成绩区 1257~1816px、
   批改意见 1129~2434px、追问 435~3760px），固定分组必然一边倒，单张「摆渡卡」
   也救不了（追问最大 3760px，比两栏净高差还大，放哪边哪边爆）。
   所以：成绩区钉在左栏（跟题目/照片同属「材料」）、批改按钮钉在右栏（动作入口），
   剩下两块大内容（逐段批注+提分建议、追问）按「大的先放、放去当前更矮的一侧」贪心分配。
   只在桌面三栏（≥1100px）生效：手机/平板是单栏靠 CSS order 保序，挪父容器不影响阅读顺序。
   批改流式进行中不摆（内容在长，会来回跳）；两栏等宽（320px），卡片换边不改高度 → 结果稳定。 */
var BAL_SWING = ['#resultArea', '#qaCard'];

function balanceCols() {
  if (window.innerWidth < 1100 || (typeof state !== 'undefined' && state.streaming)) return;
  var colL = document.querySelector('.detail-cols .col-left');
  var colR = document.querySelector('.detail-cols .col-right');
  if (!colL || !colR) return;

  var hOf = function (sel) {
    var el = document.querySelector(sel);
    if (!el || el.classList.contains('hidden')) return null;
    return { el: el, h: el.getBoundingClientRect().height + 12 };
  };

  var tot = { L: 0, R: 0 };
  ['#promptCard', '#imgCard', '#scoreArea'].forEach(function (s) { var x = hOf(s); if (x) tot.L += x.h; });
  ['#reviseCard'].forEach(function (s) { var x = hOf(s); if (x) tot.R += x.h; });

  var items = BAL_SWING.map(hOf).filter(Boolean);
  items.sort(function (a, b) { return b.h - a.h; });          /* 大的先放（LPT 贪心） */
  var put = { L: [], R: [] };
  items.forEach(function (it) {
    var k = tot.L <= tot.R ? 'L' : 'R';
    tot[k] += it.h;
    put[k].push(it.el);
  });

  /* 同一栏内按固定顺序摆（成绩→意见→追问的语义顺序），避免每次算完顺序乱跳 */
  [['L', colL], ['R', colR]].forEach(function (pair) {
    var k = pair[0], col = pair[1];
    put[k].sort(function (a, b) {
      return BAL_SWING.indexOf('#' + a.id) - BAL_SWING.indexOf('#' + b.id);
    });
    put[k].forEach(function (el) { col.appendChild(el); });
  });
}

var _balT;
window.addEventListener('resize', function () {
  clearTimeout(_balT);
  _balT = setTimeout(balanceCols, 200);
});

/* 延迟重摆：刚进详情页时照片/追问列表还没渲染完，那一刻量到的高度是旧的
   → 会分错边（真机实测：《我的小天地》自动 L1589/R3618，等 2.5s 手动再摆一次
   是 L2677/R2530）。内容渲染完高度才准，所以在打开详情后补摆两次。 */
var _balT2 = [];
function balanceSoon() {
  _balT2.forEach(clearTimeout);
  _balT2 = [setTimeout(balanceCols, 400), setTimeout(balanceCols, 1500)];
}

function deds2total(list) {
  return (list || []).reduce(function (a, x) { return a + (x.points || 0); }, 0);
}

function bindDetail() {
  $('#btnBack').addEventListener('click', function () { show('list'); loadList(); });
  $('#btnDel').addEventListener('click', function () {
    if (!confirm('确定删除这篇作文？')) return;
    fetch('/api/essays/' + encodeURIComponent(state.id), { method: 'DELETE' })
      .then(function () { toast('已删除'); show('list'); loadList(); });
  });
  $('#btnToggleImg').addEventListener('click', function () {
    var b = $('#imgBody');
    b.classList.toggle('hidden');
    $('#btnToggleImg').classList.toggle('open', !b.classList.contains('hidden'));
  });
  $('#btnTogglePrompt').addEventListener('click', function () {
    var b = $('#promptBody');
    b.classList.toggle('hidden');
    $('#btnTogglePrompt').classList.toggle('open', !b.classList.contains('hidden'));
  });
  $('#btnEdit').addEventListener('click', function () {
    $('#dText').classList.add('hidden');
    $('#dEditText').classList.remove('hidden');
    $('#editActions').classList.remove('hidden');
    $('#dEditText').focus();
  });
  $('#btnCancelEdit').addEventListener('click', function () {
    $('#dEditText').value = (state.doc && state.doc.text) || '';
    $('#dEditText').classList.add('hidden');
    $('#editActions').classList.add('hidden');
    $('#dText').classList.remove('hidden');
  });
  $('#btnSaveEdit').addEventListener('click', function () {
    fetch('/api/essays/' + encodeURIComponent(state.id), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('#dEditText').value })
    }).then(function (r) { return r.json(); }).then(function (j) {
      toast('已保存');
      openDetail(state.id);
    });
  });
  $('#btnRevise').addEventListener('click', doRevise);
  bindAnn();
  bindQa();
}

function doRevise() {
  /* B 方案：批改过程实时可见 —— 用 fetch + ReadableStream 收 SSE，
     边跑边把结果一项项渲染出来（不是等 60 秒干瞪眼）。
     EventSource 不支持 POST，所以手解 SSE。 */
  state.streaming = true;
  state.live = {};
  state.tLastField = 0;        // 最后一个 field 事件到达的时刻（诊断用）
  state.tContentDone = 0;      // 「内容到齐」的时刻
  state.announced = false;     // 已经宣布过完成，别再弹第二次
  renderResult(null, 'revising');
  $('#reviseHint').innerHTML =
    '<span id="liveStage"><span class="loading"></span> 正在准备…</span>' +
    '　<button class="btn tiny ghost" id="btnCancelRevise">✕ 取消</button>';
  $('#btnCancelRevise').addEventListener('click', doCancelRevise);

  var ctl = ('AbortController' in window) ? new AbortController() : null;

  fetch('/api/essays/' + encodeURIComponent(state.id) + '/revise_stream', {
    method: 'POST',
    signal: ctl ? ctl.signal : undefined
  }).then(function (res) {
    // ⚠️ 202 也满足 res.ok！没配 AI key 时后端返回 202 + 一段 JSON（不是事件流），
    //    不特判就会被当成「流跑完了却没收到 done」→ 误报「批改中断了」。
    if (res.status === 202) {
      return res.json().then(function (j) {
        state.streaming = false;
        toast(j.message || '还没配置 AI 接口', 5000);
        openDetail(state.id);
      });
    }
    if (!res.ok) return res.json().then(function (j) { throw new Error(j.error || '批改失败'); });
    if (!res.body) throw new Error('浏览器不支持流式响应，请换新版浏览器');

    var reader = res.body.getReader();
    var dec = new TextDecoder('utf-8');
    var buf = '';

    function pump() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buf += dec.decode(r.value, { stream: true });

        // SSE 用空行分事件
        var parts = buf.split('\n\n');
        buf = parts.pop();                 // 最后一段可能没读完，留着
        parts.forEach(function (blk) {
          var ev = '', data = '';
          blk.split('\n').forEach(function (ln) {
            if (ln.indexOf('event:') === 0) ev = ln.slice(6).trim();
            else if (ln.indexOf('data:') === 0) data += ln.slice(5).trim();
          });
          if (!ev) return;
          // 坏帧别把整条流弄死 —— 解析失败就丢这一帧，流继续走
          var payload;
          try { payload = data ? JSON.parse(data) : {}; }
          catch (e) { console.warn('坏帧已丢弃:', data); return; }
          onStreamEvent(ev, payload);
        });
        return pump();
      });
    }
    return pump();
  }).then(function () {
    // 流自然结束但没收到 done → 当中断处理，别让界面卡在「批改中」
    if (state.streaming) { toast('批改中断了'); openDetail(state.id); }
  }).catch(function (e) {
    if (e && e.name === 'AbortError') return;    // 用户点的取消，别报错
    toast('批改失败：' + (e && e.message ? e.message : e), 5000);
    openDetail(state.id);
  }).then(function () {
    state.streaming = false;
  });
}

function doCancelRevise() {
  if (!state.streaming) return;
  var el = $('#liveStage');
  if (el) el.innerHTML = '<span class="loading"></span> 正在取消…';
  fetch('/api/essays/' + encodeURIComponent(state.id) + '/revise_cancel', { method: 'POST' })
    .catch(function () {});
}

function onStreamEvent(ev, d) {
  if (ev === 'stage') {
    var el = $('#liveStage');
    if (el) el.innerHTML = '<span class="loading"></span> ' + esc(d.msg || '');
    return;
  }
  if (ev === 'think') {
    // 模型在「思考」——后端默认已关思考（思考曾占 60~80% 耗时），
    // 留着这个事件是防以后开思考时界面几十秒死寂一片。
    var el2 = $('#liveStage');
    if (el2) el2.innerHTML = '<span class="loading"></span> 💭 AI 思考中…（' + (d.n || 0) + ' 字思路）';
    return;
  }
  if (ev === 'text') {
    // 识别出的正文 —— 一抄完就显示，不用等批改。
    // part: 'text'/'title' 是边抄边推的单项，'all' 是抄完后补发的最终版
    //（模型吐的 JSON 若被 parse_json 修过，单项可能没推全，靠 'all' 兜底）。
    var txt = (d.part === 'all' || d.part === 'text') ? d.text : null;
    var ttl = (d.part === 'all' || d.part === 'title') ? d.title : null;
    if (txt) {
      $('#dText').classList.remove('hidden');
      $('#dText').innerHTML = esc(txt).replace(/\n/g, '<br>');
      state.doc.text = txt;
    }
    if (ttl) {
      var t = $('#dTitle'); if (t) t.textContent = ttl;
    }
    return;
  }
  if (ev === 'field') {
    state.live[d.key] = d.value;
    state.tLastField = Date.now();
    renderResult(state.live, 'revising');   // 边跑边长，和最终长一样
    /* ⚡ 内容一出完就宣布完成：`revised` 是结果 JSON 的**最后一个字段**，它到了
       就说明内容已经齐了。不必等后端收线 —— 后端在 done 之前还要 save_essay
       （fsync 在 NAS 上肉眼可见地慢），用户会以为卡住了（体感反馈的「延迟」）。
       之后收到真正的 done 只做静默对账，不再弹第二次。 */
    if (d.key === 'revised' && state.live.score != null && !state.announced) {
      state.announced = true;
      state.tContentDone = Date.now();
      state.streaming = false;
      renderResult(state.live, 'done');
      toast('批改完成');
    }
    return;
  }
  if (ev === 'ann') {
    $('#dText').innerHTML = renderAnnotatedText(state.doc.text || '', d.annotations || []);
    renderAnnPanel();
    return;
  }
  if (ev === 'cancelled') {
    state.streaming = false;
    toast('已取消，改动没有保存');
    openDetail(state.id);
    return;
  }
  if (ev === 'error') {
    state.streaming = false;
    toast('批改失败：' + (d.error || ''), 5000);
    openDetail(state.id);
    return;
  }
  if (ev === 'done') {
    // 诊断留痕：内容到齐 → 后端 done 之间差多久（区分是接口慢还是我们慢）
    var gap = state.tContentDone ? (Date.now() - state.tContentDone) : -1;
    if (gap >= 0) console.log('[timing] 内容到齐→后端 done: ' + gap + ' ms');
    state.streaming = false;
    if (!state.announced) toast('批改完成');   // 没提前宣布过才由这里兜底
    openDetail(state.id);   // 重新拉一遍，让界面回到稳定态
    return;
  }
}

/* ---------------- 继续追问（答疑） ----------------
 * 和批改同一个套路：POST + 手解 SSE，回答逐字冒出来。
 * ⚠️ 不跟 doRevise 共用解析：那边跟「取消/还原/state.streaming」缠在一起，
 *    抽出来反而容易把已经验证过的批改流程改坏。这里单独一份 streamPost。 */

/* 自动跟随滚动 —— 只在用户本来就停在底部时才跟着滚。
   ⚠️ 以前每个 token 都 scrollIntoView：
      ① 用户正往上翻，屏幕被硬拽回底部 —— 「整个页面卡来卡去」；
      ② 每次 scrollIntoView 强制同步重排，两核 NAS 上肉眼可见地卡。
   现在：用户上滑 → 立刻撒手不滚；回到底部 → 恢复跟随；滚动合并到一帧最多一次。 */
var qaFollow = true;
var qaRaf = 0;
function qaMaybeScroll() {
  if (!qaFollow || qaRaf) return;
  qaRaf = requestAnimationFrame(function () {
    qaRaf = 0;
    if (!qaFollow) return;
    window.scrollTo(0, document.documentElement.scrollHeight);  // 一滚到底，不搞平滑动画
  });
}
function qaWatchScroll() {
  window.addEventListener('scroll', function () {
    var doc = document.documentElement;
    var dist = doc.scrollHeight - (window.scrollY + window.innerHeight);
    // 离底部 150px 以内算「想看最新内容」；离开就算「用户自己在翻」
    qaFollow = dist <= 150;
  }, { passive: true });
}

var QA_CHIPS = [
  '这篇为什么扣了这些分？',
  '最该改的三处是哪里？',
  '下次写这类作文，开头怎么起？',
  '你改的那几句，为什么这么改？'
];

function renderQa() {
  var d = state.doc || {};
  var card = $('#qaCard');
  var hasText = !!(d.text && d.text.trim());
  card.classList.toggle('hidden', !hasText);   // 没有正文就没什么可问的
  if (!hasText) return;

  var list = d.qa || [];
  $('#qaList').innerHTML = list.map(function (it) {
    var ans = (it.a || '').trim();
    return '<div class="qa-item">' +
        '<div class="qa-q"><span class="qa-who">问</span>' +
          '<span class="qa-qtext">' + esc(it.q) + '</span></div>' +
        '<div class="qa-a">' + (ans ? esc(ans) : '<span class="muted">（没有答完）</span>') +
          (it.interrupted && ans ? '<div class="muted small">（回答被中断）</div>' : '') +
        '</div>' +
      '</div>';
  }).join('');
  $('#qaList').classList.toggle('hidden', list.length === 0);
  $('#qaEmpty').classList.toggle('hidden', list.length > 0);
  balanceCols();   /* 追问内容长了会改变左右平衡，重摆一次 */
}

function bindQa() {
  qaWatchScroll();
  $('#qaChips').innerHTML = QA_CHIPS.map(function (q) {
    return '<button class="chip" data-q="' + esc(q) + '">' + esc(q) + '</button>';
  }).join('');
  $('#qaChips').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('.chip') : null;
    if (!b) return;
    // 只把问题填进输入框、不直接发 —— 一发就是一次真实调用，让人自己确认一下
    $('#qaText').value = b.getAttribute('data-q') || '';
    $('#qaText').focus();
  });
  $('#btnAsk').addEventListener('click', askFollowup);
  $('#btnAskStop').addEventListener('click', doStopAsk);
  $('#btnQaClear').addEventListener('click', function () {
    if (!(state.doc && (state.doc.qa || []).length)) { toast('还没有追问'); return; }
    if (!confirm('清空这篇作文的全部追问记录？')) return;
    fetch('/api/essays/' + encodeURIComponent(state.id) + '/qa', { method: 'DELETE' })
      .then(function (r) { return r.json(); })
      .then(function () { toast('已清空'); openDetail(state.id); })
      .catch(function () { toast('清空失败'); });
  });
  $('#qaText').addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); askFollowup(); }
  });
}

function doStopAsk() {
  if (!state.asking) return;
  $('#btnAskStop').disabled = true;
  fetch('/api/essays/' + encodeURIComponent(state.id) + '/ask_cancel', { method: 'POST' })
    .catch(function () {});
}

function askFollowup() {
  if (state.asking) { toast('AI 老师还在回答上一个问题'); return; }
  var q = ($('#qaText').value || '').trim();
  if (!q) { toast('先写一句你想问的'); return; }

  state.asking = true;
  qaFollow = true;                     // 新问题默认跟着看最新
  var askBtn = $('#btnAsk');
  askBtn.disabled = true;
  askBtn.textContent = '回答中…';        // 按钮本身也要给反馈，别还写着「发送」
  $('#btnAskStop').disabled = false;
  $('#btnAskStop').classList.remove('hidden');
  $('#qaText').value = '';

  /* 先把「问」的气泡和「正在回答」插上去，回答逐字往这个气泡里长。
     ⚠️ 等待第一个字的空窗期必须有**看得见**的反馈（文字 + 转圈 + 秒数），
        原来只有一个白色 spinner，落在浅色气泡上隐形 —— 用户以为没反应。
     等流结束再 openDetail 拉一遍，用盘上的正式记录替换这个临时气泡。 */
  var item = document.createElement('div');
  item.className = 'qa-item';
  item.innerHTML = '<div class="qa-q"><span class="qa-who">问</span><span class="qa-qtext"></span></div>' +
                   '<div class="qa-a live"><span class="loading"></span>' +
                   '<span class="qa-thinking">AI 老师正在思考…</span></div>';
  item.querySelector('.qa-qtext').textContent = q;   // 用户输入一律 textContent，不拼 HTML
  $('#qaList').classList.remove('hidden');
  $('#qaEmpty').classList.add('hidden');
  $('#qaList').appendChild(item);
  var out = item.querySelector('.qa-a');
  var got = false;

  /* 秒数每秒刷新 —— 让人知道它卡着没死机，是真的在等 AI */
  var t0 = Date.now();
  var thinkEl = item.querySelector('.qa-thinking');
  var tick = setInterval(function () {
    if (thinkEl) {
      thinkEl.textContent = 'AI 老师正在思考…（已等 ' +
        Math.round((Date.now() - t0) / 1000) + ' 秒）';
    }
  }, 1000);

  function finish() {
    clearInterval(tick);
    state.asking = false;
    askBtn.disabled = false;
    askBtn.textContent = '发送';
    $('#btnAskStop').disabled = false;
    $('#btnAskStop').classList.add('hidden');
  }

  /* 没问成（超时/出错/没配 key）时把问题放回输入框 —— 别让用户白打一遍字 */
  function restoreDraft() {
    if (!$('#qaText').value.trim()) $('#qaText').value = q;
  }

  // 用 textContent 累加，不做任何 HTML 拼接 —— 回答是模型吐的纯文本
  streamPost('/api/essays/' + encodeURIComponent(state.id) + '/ask', { q: q },
    function (ev, d) {
      if (ev === 'delta') {
        if (!got) {
          clearInterval(tick);        // 第一个字到了，「思考中」退场
          out.textContent = '';
          got = true;
        }
        out.textContent += (d.t || '');
        qaMaybeScroll();               // 用户在往上翻就不滚，别拽他屏幕
      } else if (ev === 'queue') {
        clearInterval(tick);
        toast(d.message || '还没配置 AI 接口', 5000);
        item.remove();
        restoreDraft();
      } else if (ev === 'cancelled') {
        clearInterval(tick);
        if (thinkEl && !got) thinkEl.textContent = '（已停止，没有答完）';
        toast('已停止');
      } else if (ev === 'error') {
        clearInterval(tick);
        if (thinkEl && !got) thinkEl.textContent = '（没答出来）';
        toast('追问失败：' + (d.error || ''), 5000);
        restoreDraft();
      }
    }
  ).catch(function (e) {
    toast('追问失败：' + (e && e.message ? e.message : e), 5000);
    restoreDraft();
  }).then(function () {
    finish();
    // 答完 / 中断 / 出错都回后端拉一遍：界面上看到的必须和盘上存的一致
    openDetail(state.id);
  });
}

/* POST + 手解 SSE（EventSource 只支持 GET，而追问要带 body）。
   onEvent(ev, data) 由调用方处理；202（没配 key）以 'queue' 事件回调。 */
function streamPost(url, body, onEvent) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  }).then(function (res) {
    // ⚠️ 202 也满足 res.ok！没配 key 时返回的是 202 + JSON（不是事件流），
    //    不特判会被当成「流跑完却没收到东西」。
    if (res.status === 202) {
      return res.json().then(function (j) { onEvent('queue', j); });
    }
    if (!res.ok) {
      return res.json().then(function (j) {
        var err = new Error(j.error || '请求失败');
        throw err;
      });
    }
    if (!res.body) throw new Error('浏览器不支持流式响应，请换新版浏览器');

    var reader = res.body.getReader();
    var dec = new TextDecoder('utf-8');
    var buf = '';
    function pump() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buf += dec.decode(r.value, { stream: true });
        var parts = buf.split('\n\n');   // SSE 用空行分事件
        buf = parts.pop();               // 最后一段可能没读完，留着
        parts.forEach(function (blk) {
          var ev = '', data = '';
          blk.split('\n').forEach(function (ln) {
            if (ln.indexOf('event:') === 0) ev = ln.slice(6).trim();
            else if (ln.indexOf('data:') === 0) data += ln.slice(5).trim();
          });
          if (!ev) return;
          var payload;
          try { payload = data ? JSON.parse(data) : {}; }
          catch (e) { console.warn('坏帧已丢弃:', data); return; }
          onEvent(ev, payload);
        });
        return pump();
      });
    }
    return pump();
  });
}

/* 设置口令门已移除 —— 开源版只有站点口令一道门（见 /api/login）。 */
function cfgCatch(e, msg) {
  if (e && e.code === 'pw_cancel') return;
  toast(msg || '操作失败');
}

/* ---------------- 设置 ---------------- */
var PRESETS = {
  mimo: { url: 'https://token-plan-cn.xiaomimimo.com/v1', model: 'mimo-v2.6-flash' },
  deepseek: { url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  qwen: { url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' }
};

function fillCfg(c) {
  $('#cfgUrl').value = c.base_url || '';
  $('#cfgModel').value = c.model || '';
  $('#cfgKey').value = '';
  $('#cfgKey').placeholder = c.has_key ? ('已保存：' + c.key_hint + '（留空则不修改）') : 'sk-...';
  var ap = (c.profiles || [])[c.active_profile || 0];
  $('#cfgName').value = ap ? (ap.name || '') : '';
  var mode = (c.grade_mode === 'deep') ? 'deep' : 'fast';
  var radios = document.querySelectorAll('input[name="gradeMode"]');
  for (var i = 0; i < radios.length; i++) radios[i].checked = (radios[i].value === mode);
  var strict = (c.grade_strict === 'standard') ? 'standard' : 'strict';   // 缺省=严格
  var sradios = document.querySelectorAll('input[name="gradeStrict"]');
  for (var j = 0; j < sradios.length; j++) sradios[j].checked = (sradios[j].value === strict);
  renderProfiles(c);
}

function renderProfiles(c) {
  var box = $('#cfgProfiles');
  var list = c.profiles || [];
  var act = c.active_profile || 0;
  box.innerHTML = list.map(function (p, i) {
    var on = (i === act);
    return '<div class="pf-row' + (on ? ' on' : '') + '">' +
      '<div class="pf-main"><b>' + esc(p.name || '未命名') + '</b>' +
      '<div class="muted small">' + esc(p.model || '') + (p.has_key ? ' · Key 已存' : '') + '</div></div>' +
      (on ? '<span class="pf-tag">使用中</span>'
          : '<button class="btn ghost pf-use" data-i="' + i + '">启用</button>' +
            '<button class="btn ghost pf-del" data-i="' + i + '">删除</button>') +
      '</div>';
  }).join('');
  function post(body, msg) {
    fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) })
      .then(function () { return fetch('/api/config'); })
      .then(function (r) { return r.json(); })
      .then(function (c2) { fillCfg(c2); toast(msg); })
      .catch(function (e) { cfgCatch(e, '操作失败'); });
  }
  box.querySelectorAll('.pf-use').forEach(function (b) {
    b.addEventListener('click', function () { post({ activate: +b.dataset.i }, '已切换配置'); });
  });
  box.querySelectorAll('.pf-del').forEach(function (b) {
    b.addEventListener('click', function () { post({ delete_profile: +b.dataset.i }, '已删除这套配置'); });
  });
}

function cfgForm() {
  var body = { base_url: $('#cfgUrl').value.trim(), model: $('#cfgModel').value.trim() };
  if ($('#cfgKey').value.trim()) body.api_key = $('#cfgKey').value.trim();
  return body;
}


function bindCfg() {
  $('#btnCfg').addEventListener('click', function () {
    fetch('/api/config').then(function (r) { return r.json(); })
      .then(function (c) {
        fillCfg(c);
        show('cfg');
      }).catch(function (e) { cfgCatch(e, '读取设置失败'); });
  });
  $('#btnBackCfg').addEventListener('click', function () { show('list'); });
  $('#cfgPreset').addEventListener('change', function () {
    var p = PRESETS[$('#cfgPreset').value];
    if (p) { $('#cfgUrl').value = p.url; $('#cfgModel').value = p.model; }   // Key 仍要自己填
  });
  $('#btnAddCfg').addEventListener('click', function () {
    var body = cfgForm();
    body.add_profile = { name: $('#cfgName').value.trim(), base_url: body.base_url,
                         model: body.model, api_key: body.api_key || '' };
    fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) })
      .then(function () { return fetch('/api/config'); })
      .then(function (r) { return r.json(); })
      .then(function (c2) { fillCfg(c2); toast('已存为新配置并启用'); })
      .catch(function (e) { cfgCatch(e, '保存失败'); });
  });
  $('#btnSaveCfg').addEventListener('click', function () {
    var body = cfgForm();
    if ($('#cfgName').value.trim()) body.name = $('#cfgName').value.trim();
    var sel = document.querySelector('input[name="gradeMode"]:checked');
    body.grade_mode = sel ? sel.value : 'fast';
    var ssel = document.querySelector('input[name="gradeStrict"]:checked');
    body.grade_strict = ssel ? ssel.value : 'strict';
    fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function () { toast('设置已保存'); show('list'); })
      .catch(function (e) { cfgCatch(e, '保存失败'); });
  });
}

/* ---------------- AI 学情分析 ---------------- */
function renderAnalyze(d) {
  $('#anOverview').textContent = d.overview || '（无概述）';
  $('#anCount').textContent = '基于 ' + d.count + ' 篇已批改作文';

  // 共性问题
  var probs = d.common_problems || [];
  $('#anProblems').innerHTML = probs.length
    ? probs.map(function (p, i) {
        return '<div class="an-item">' +
          '<div class="an-item-head"><span class="an-badge">' + (i + 1) + '</span>' +
          '<strong>' + esc(p.problem || '') + '</strong>' +
          '<span class="an-freq">' + esc(p.frequency || '') + '</span></div>' +
          '<div class="an-detail">' + esc(p.detail || '') + '</div>' +
          '<div class="an-fix">💡 ' + esc(p.fix || '') + '</div>' +
        '</div>';
      }).join('')
    : '<div class="muted">没有找到明显的共性问题</div>';

  // 精品作文
  var exc = d.excellent || [];
  $('#anExcellent').innerHTML = exc.length
    ? exc.map(function (e) {
        return '<div class="an-item an-good">' +
          '<div class="an-item-head"><span class="an-badge good">⭐</span>' +
          '<strong>' + esc(e.title || '') + '</strong>' +
          '<span class="an-score">' + esc(e.score) + ' 分</span></div>' +
          '<div class="an-detail">' + esc(e.reason || '') + '</div>' +
        '</div>';
      }).join('')
    : '<div class="muted">暂无精品</div>';

  // 需要关注
  var nw = d.needs_work || [];
  $('#anNeedsWork').innerHTML = nw.length
    ? nw.map(function (e) {
        return '<div class="an-item an-warn">' +
          '<div class="an-item-head"><span class="an-badge warn">⚠️</span>' +
          '<strong>' + esc(e.title || '') + '</strong>' +
          '<span class="an-score">' + esc(e.score) + ' 分</span></div>' +
          '<div class="an-detail">' + esc(e.reason || '') + '</div>' +
        '</div>';
      }).join('')
    : '<div class="muted">暂无需要特别关注的</div>';

  // 教学建议
  var tips = d.teaching_tips || [];
  $('#anTips').innerHTML = tips.length
    ? '<ul class="an-tips">' + tips.map(function (t) {
        return '<li>' + esc(t) + '</li>';
      }).join('') + '</ul>'
    : '<div class="muted">暂无建议</div>';

  $('#analyzeLoading').classList.add('hidden');
  $('#analyzeError').classList.add('hidden');
  $('#analyzeResult').classList.remove('hidden');
}

function runAnalyze() {
  $('#analyzeLoading').classList.remove('hidden');
  $('#analyzeError').classList.add('hidden');
  $('#analyzeResult').classList.add('hidden');
  show('analyze');

  fetch('/api/analyze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}'
  }).then(function (r) {
    return r.json().then(function (d) {
      if (!r.ok || d.error) {
        $('#analyzeError').textContent = d.error || ('请求失败（' + r.status + '）');
        $('#analyzeError').classList.remove('hidden');
        $('#analyzeLoading').classList.add('hidden');
        return;
      }
      renderAnalyze(d);
    });
  }).catch(function (e) {
    $('#analyzeError').textContent = '网络错误：' + e.message;
    $('#analyzeError').classList.remove('hidden');
    $('#analyzeLoading').classList.add('hidden');
  });
}

function bindAnalyze() {
  $('#btnAnalyze').addEventListener('click', runAnalyze);
  $('#btnBackAnalyze').addEventListener('click', function () { show('list'); loadList(); });
}


/* ---------------- 软件更新 ---------------- */
var _latestVer = null;   // 记住最新版本号，供「立即更新」用

function bindUpdate() {
  var btnCheck = $('#btnCheckUpdate');
  var btnApply = $('#btnApplyUpdate');
  if (!btnCheck) return;

  // ⚠️ 页面加载时**不发任何更新请求**：
  //    ① 未登录打 /api/update/check 必 401（控制台报错 + 拖慢 networkidle）
  //    ② 该接口服务端要真连 GitHub（约 1s），不该挡页面加载
  //    版本号走轻量 /api/version，且只在打开设置页时才拉。
  function loadVersion() {
    fetch('/api/version').then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok) $('#curVer').textContent = 'v' + d.version;
      }).catch(function () {
        $('#curVer').textContent = 'v?';
      });
  }
  $('#btnCfg').addEventListener('click', loadVersion);

  btnCheck.addEventListener('click', function () {
    btnCheck.disabled = true;
    btnCheck.textContent = '正在检查…';
    fetch('/api/update/check').then(function (r) { return r.json(); })
      .then(function (d) {
        btnCheck.disabled = false;
        btnCheck.textContent = '检查更新';
        if (!d.ok) { toast('检查失败：' + d.error); return; }
        $('#curVer').textContent = 'v' + d.current;
        var st = $('#updateStatus');
        st.style.display = '';
        if (d.has_update) {
          _latestVer = d.latest;
          st.innerHTML = '🎉 发现新版本 <strong>v' + d.latest + '</strong>（当前 v' + d.current + '）';
          btnApply.classList.remove('hidden');
          btnApply.textContent = '立即更新到 v' + d.latest;
          var rel = $('#updateRelease');
          if (d.body) {
            rel.classList.remove('hidden');
            rel.innerHTML = '<strong>更新内容：</strong><br>' +
              esc(d.body).replace(/\n/g, '<br>');
          }
        } else {
          st.innerHTML = '✅ 已是最新版（v' + d.current + '）';
          btnApply.classList.add('hidden');
          $('#updateRelease').classList.add('hidden');
        }
      }).catch(function (e) {
        btnCheck.disabled = false;
        btnCheck.textContent = '检查更新';
        toast('检查失败：' + e.message);
      });
  });

  btnApply.addEventListener('click', function () {
    if (!_latestVer) return;
    if (!confirm('确定更新到 v' + _latestVer + '？\n\n更新过程约 10~30 秒，期间网站会短暂中断。\n当前代码会自动备份到 data/backups/。')) return;
    btnApply.disabled = true;
    btnApply.textContent = '正在更新，请勿关闭页面…';
    fetch('/api/update/apply', { method: 'POST' })
      .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
      .then(function (res) {
        if (!res.data.ok) {
          btnApply.disabled = false;
          btnApply.textContent = '立即更新到 v' + _latestVer;
          toast('更新失败：' + res.data.error);
          return;
        }
        toast('更新完成！页面将在 5 秒后自动刷新…');
        setTimeout(function () { location.reload(); }, 5000);
      }).catch(function () {
        // 进程被杀掉重启，连接断开是正常的
        toast('更新中，页面即将自动刷新…');
        setTimeout(function () { location.reload(); }, 5000);
      });
  });
}

/* ---------------- 改口令 ---------------- */
function bindPw() {
  var btn = $('#btnSavePw');
  if (!btn) return;
  btn.addEventListener('click', function () {
    var npw = $('#cfgNewPw').value;
    // 留空不是「取消口令」——没口令 = 整站锁定，这里直接拦下
    if (!npw.trim()) { toast('口令不能为空'); $('#cfgNewPw').focus(); return; }
    btn.disabled = true;
    fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_password: npw })
    }).then(function (r) {
      btn.disabled = false;
      if (!r.ok) throw new Error('服务器返回 ' + r.status);
      $('#cfgNewPw').value = '';
      toast('口令已修改，下次访问要用新口令');
    }).catch(function (e) {
      btn.disabled = false;
      toast('改口令失败：' + e.message);
    });
  });
}

/* ---------------- 启动 ---------------- */
document.addEventListener('DOMContentLoaded', function () {
  bindNew(); bindDetail(); bindCfg(); bindAnalyze(); bindPw(); bindUpdate();
  // 停在登录页时**别去拉列表**：拉了必 401，配合上面的 reload 就是无限刷新循环
  if (!(window.__onLoginPage && window.__onLoginPage())) loadList();
});
