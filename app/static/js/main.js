// 前台通用交互：移动端导航折叠
(function() {
    var toggle = document.getElementById('navToggle');
    var links = document.getElementById('navLinks');
    if (!toggle || !links) return;

    function setOpen(open) {
        links.classList.toggle('show', open);
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
    }

    toggle.addEventListener('click', function(e) {
        e.stopPropagation();
        setOpen(!links.classList.contains('show'));
    });

    // 点击菜单外区域关闭
    document.addEventListener('click', function(e) {
        if (!links.classList.contains('show')) return;
        if (links.contains(e.target) || toggle.contains(e.target)) return;
        setOpen(false);
    });

    // 选择菜单项后关闭
    links.addEventListener('click', function(e) {
        if (e.target.closest('a')) setOpen(false);
    });

    // Esc 关闭
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') setOpen(false);
    });

    // 回到桌面宽度时复位
    window.addEventListener('resize', function() {
        if (window.innerWidth > 720) setOpen(false);
    });
})();

// CSP 兼容：不再使用内联 onchange，改用 data-auto-submit 统一绑定
document.querySelectorAll('[data-auto-submit]').forEach(function(select) {
    select.addEventListener('change', function() {
        if (select.form) select.form.submit();
    });
});

// 轻量提示条：前台操作失败等反馈，避免原生 alert 打断浏览
window.amigToast = function(message, type) {
    if (!message) return;
    var host = document.getElementById('toastHost');
    if (!host) {
        host = document.createElement('div');
        host.id = 'toastHost';
        host.className = 'toast-host';
        host.setAttribute('role', 'status');
        host.setAttribute('aria-live', 'polite');
        document.body.appendChild(host);
    }
    var el = document.createElement('div');
    el.className = 'toast' + (type === 'error' ? ' toast-error' : '');
    el.textContent = message;
    host.appendChild(el);
    requestAnimationFrame(function() { el.classList.add('show'); });
    setTimeout(function() {
        el.classList.remove('show');
        setTimeout(function() {
            if (el.parentNode) el.parentNode.removeChild(el);
        }, 220);
    }, 3200);
};

// 图片加载失败时隐藏破图图标，容器显示占位说明（文件缺失由运维工具排查）
function markImageFailed(img) {
    if (!img || img.tagName !== 'IMG' || !img.getAttribute('src') || img.dataset.loadFailed) return;
    img.dataset.loadFailed = '1';
    img.classList.add('img-load-failed');
    var holder = img.closest('.card-img-wrap, .detail-stage');
    if (holder) holder.classList.add('img-failed');
    if (!img.alt) img.alt = '图片加载失败';
}

document.addEventListener('error', function(event) {
    markImageFailed(event.target);
}, true);

// 兜底：本脚本执行前就已失败的图片不会再触发 error 事件，按加载结果补标记
document.querySelectorAll('img').forEach(function(img) {
    if (img.getAttribute('src') && img.complete && img.naturalWidth === 0) markImageFailed(img);
});
