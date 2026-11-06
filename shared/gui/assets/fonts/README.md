# Bundled fonts

Place redistributable `.ttf` or `.otf` assets here (subdirectories are supported),
along with their license and attribution files. The GUI registers them with Qt
once at startup, without installing them on the host. Preferred families are
Red Hat Text for interface text and JetBrains Mono for technical text.

No font binaries are bundled yet. Until licensed assets are added, the GUI uses
installed alternatives, then Qt's system fonts. Keep labels wrapped and layouts
flexible: fonts, display scaling, and text measurements still vary across hosts.
