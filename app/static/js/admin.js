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
