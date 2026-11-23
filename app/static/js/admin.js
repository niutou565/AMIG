// 后台通用交互：移动端侧栏抽屉
(function() {
    var toggle = document.getElementById('adminNavToggle');
    var sidebar = document.getElementById('adminSidebar');
    var overlay = document.getElementById('adminOverlay');
    if (!toggle || !sidebar || !overlay) return;

    function setOpen(open) {
        sidebar.classList.toggle('show', open);
        overlay.hidden = !open;
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
        document.body.style.overflow = open ? 'hidden' : '';
    }

    toggle.addEventListener('click', function() {
        setOpen(!sidebar.classList.contains('show'));
    });
    overlay.addEventListener('click', function() {
        setOpen(false);
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') setOpen(false);
    });
    sidebar.addEventListener('click', function(e) {
        if (e.target.closest('a')) setOpen(false);
    });
    window.addEventListener('resize', function() {
        if (window.innerWidth > 900) setOpen(false);
    });
})();

// CSP 兼容：内联 onchange 改为 data-auto-submit
document.querySelectorAll('[data-auto-submit]').forEach(function(select) {
    select.addEventListener('change', function() {
        if (select.form) select.form.submit();
    });
});

// CSP 兼容：删除确认统一通过 data-confirm 绑定（支持 form 或 submit 按钮）
document.addEventListener('submit', function(event) {
    var form = event.target;
    if (!form || !form.matches) return;
    var submitter = event.submitter;
    var message = (submitter && submitter.dataset ? submitter.dataset.confirm : '') ||
        (form.dataset ? form.dataset.confirm : '');
    if (message && !window.confirm(message)) {
        event.preventDefault();
    }
});

// 表单提交状态：提交期间显示“处理中…”并阻止重复提交。
// 只改变视觉状态，不设置 disabled，避免按钮的 name/value 从表单数据中丢失。
var SUBMIT_LOCK_TIMEOUT_MS = 20000;

function clearSubmitState(form) {
    if (!form) return;
    delete form.dataset.submitting;
    if (form._submitTimer) {
        window.clearTimeout(form._submitTimer);
        form._submitTimer = null;
    }
    form.querySelectorAll('.is-loading').forEach(function(btn) {
        btn.classList.remove('is-loading');
        btn.removeAttribute('aria-busy');
        if (btn.dataset.originalText) {
            btn.textContent = btn.dataset.originalText;
            delete btn.dataset.originalText;
        }
    });
}

document.addEventListener('submit', function(event) {
    var form = event.target;
    if (!form || !form.matches) return;
    if (form.dataset.submitting === '1') {
        event.preventDefault();
        return;
    }
    // 已被确认框或页面自身脚本取消时不进入提交态
    if (event.defaultPrevented) return;
    form.dataset.submitting = '1';
    var btn = event.submitter ||
        form.querySelector('button[type="submit"], input[type="submit"]');
    if (btn) {
        btn.classList.add('is-loading');
        btn.setAttribute('aria-busy', 'true');
        if (btn.tagName === 'BUTTON' && !btn.dataset.originalText) {
            btn.dataset.originalText = btn.textContent;
            btn.textContent = '处理中…';
        }
    }
    // 请求被中断或长时间无响应时自动恢复，避免按钮永久停留在“处理中…”
    form._submitTimer = window.setTimeout(function() {
        clearSubmitState(form);
    }, SUBMIT_LOCK_TIMEOUT_MS);
});

// 浏览器前进/后退恢复页面时，重置可能残留的提交中状态
window.addEventListener('pageshow', function() {
    document.querySelectorAll('form[data-submitting="1"]').forEach(function(form) {
        clearSubmitState(form);
    });
});
