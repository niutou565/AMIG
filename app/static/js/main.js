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
