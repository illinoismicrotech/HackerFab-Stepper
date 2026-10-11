"""Look and feel: palettes, fonts, DPI awareness and pixel scaling.

Every size in the UI is written for a 96 DPI screen and passed through px(), so the
layout keeps its proportions on high-DPI laptops (125 %, 150 % Windows scaling) where the
app runs DPI-aware and Windows no longer stretches (blurs) the window.
"""
import platform
import tkinter.font as tkfont

import ttkbootstrap as ttk
from ttkbootstrap.style import ThemeDefinition

# ttkbootstrap colour roles. 'dark' doubles as the neutral button colour.
PALETTES = {
    'studio-dark': ('dark', {
        'primary': '#2a9d8f', 'secondary': '#8b949e', 'success': '#2ea043',
        'info': '#3b82c4', 'warning': '#c58a1a', 'danger': '#da3633',
        'light': '#e6e9ed', 'dark': '#343b45', 'bg': '#1c2026',
        'fg': '#e6e9ed', 'selectbg': '#2f3741', 'selectfg': '#ffffff',
        'border': '#3a424d', 'inputfg': '#e6e9ed', 'inputbg': '#262b33',
        'active': '#2b323b'}),
    'studio': ('light', {
        'primary': '#1f7a6f', 'secondary': '#5f6b76', 'success': '#1a7f37',
        'info': '#2f6fb0', 'warning': '#9a6700', 'danger': '#cf222e',
        'light': '#f6f8fa', 'dark': '#d5dbe1', 'bg': '#f4f6f8',
        'fg': '#1f2328', 'selectbg': '#d8dee4', 'selectfg': '#1f2328',
        'border': '#c9d1d9', 'inputfg': '#1f2328', 'inputbg': '#ffffff',
        'active': '#e7ebef'}),
}

# Colours for things ttkbootstrap has no role for. Same keys for both themes.
SEMANTIC = {
    'dark': {
        'text': '#e6e9ed', 'muted': '#98a2ad', 'faint': '#6b7580', 'border': '#333a44',
        'surface': '#232830', 'hover': '#2c333c', 'canvas': '#0d0f12', 'accent': '#2a9d8f',
        'red': '#ff6b6b', 'red_bg': '#5a1e22', 'uv': '#a594ff', 'uv_bg': '#3a2f73',
        'off': '#9aa3ad', 'off_bg': '#2e343c', 'ok': '#3fb950', 'ok_bg': '#16361f',
        'warn': '#e3a33a', 'warn_bg': '#4a3510', 'bad': '#ff7b72', 'bad_bg': '#5c1d1b',
        'info': '#79b8ff', 'info_bg': '#173452', 'seg_bg': '#262c34', 'seg_on': '#2a9d8f',
    },
    'light': {
        'text': '#1f2328', 'muted': '#59636e', 'faint': '#8c959f', 'border': '#d0d7de',
        'surface': '#ffffff', 'hover': '#eaeef2', 'canvas': '#0d0f12', 'accent': '#1f7a6f',
        'red': '#c62828', 'red_bg': '#ffe1e0', 'uv': '#5b3fd6', 'uv_bg': '#e6e0ff',
        'off': '#59636e', 'off_bg': '#e6e9ec', 'ok': '#1a7f37', 'ok_bg': '#dafbe1',
        'warn': '#9a6700', 'warn_bg': '#fff1c7', 'bad': '#cf222e', 'bad_bg': '#ffe0e0',
        'info': '#0969da', 'info_bg': '#ddf4ff', 'seg_bg': '#e6e9ec', 'seg_on': '#1f7a6f',
    },
}

_state = {'scale': 1.0, 'kind': 'dark'}


def enable_dpi_awareness() -> bool:
    """Ask Windows for real pixels (per-monitor DPI aware) before any window exists.

    Without it Windows bitmap-stretches the whole app on scaled screens: text is blurry and,
    worse, the projector window is resampled, so patterns are not projected pixel for pixel.
    """
    if platform.system() != 'Windows':
        return False
    import ctypes
    try:
        return ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0  # per-monitor aware
    except Exception:
        try:
            return bool(ctypes.windll.user32.SetProcessDPIAware())
        except Exception:
            return False


def px(value) -> int:
    """96-DPI pixels -> screen pixels."""
    return int(round(value * _state['scale']))


def ui_scale() -> float:
    return _state['scale']


def palette() -> dict:
    """Semantic colours plus the ttkbootstrap roles of the active theme."""
    colors = dict(SEMANTIC[_state['kind']])
    try:
        style_colors = ttk.Style().colors
        for key in ('primary', 'secondary', 'success', 'info', 'warning', 'danger', 'bg', 'fg',
                    'inputbg', 'inputfg', 'selectbg', 'selectfg', 'dark', 'light'):
            colors[key] = getattr(style_colors, key)
    except Exception:
        pass
    return colors


def _font_family(root, candidates):
    available = set(tkfont.families(root))
    return next((name for name in candidates if name in available), None)


def configure_theme(root, name='studio-dark', text_scale=1.0):
    style = ttk.Style()
    for key, (kind, colors) in PALETTES.items():
        if key not in style.theme_names():
            style.register_theme(ThemeDefinition(name=key, themetype=kind, colors=colors))
    if name not in PALETTES:
        name = 'studio-dark'
    style.theme_use(name)
    _state['kind'] = PALETTES[name][0]
    try:
        _state['scale'] = max(1.0, float(root.tk.call('tk', 'scaling')) / (96 / 72))
    except Exception:
        _state['scale'] = 1.0
    try:
        text_scale = max(0.8, min(1.6, float(text_scale)))
    except (TypeError, ValueError):
        text_scale = 1.0
    family = _font_family(root, ('Segoe UI', 'Inter', 'SF Pro Text', 'Noto Sans', 'DejaVu Sans',
                                 'Liberation Sans', 'Arial')) or 'TkDefaultFont'
    mono = _font_family(root, ('Cascadia Mono', 'Consolas', 'DejaVu Sans Mono', 'Liberation Mono',
                               'Courier New')) or 'TkFixedFont'
    base = 10 * text_scale
    for font_name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont', 'TkTooltipFont'):
        tkfont.nametofont(font_name, root=root).configure(family=family, size=round(base))
    fonts = {
        'StepperTitle': (family, 15, 'bold'), 'StepperBrand': (family, 13, 'bold'),
        'StepperSection': (family, 11, 'bold'), 'StepperCaption': (family, 8, 'bold'),
        'StepperSmall': (family, 9, 'normal'), 'StepperBold': (family, 10, 'bold'),
        'StepperBig': (family, 12, 'bold'), 'StepperHuge': (family, 17, 'bold'),
        'StepperMono': (mono, 11, 'normal'), 'StepperMonoBig': (mono, 13, 'bold'),
    }
    if not hasattr(root, '_stepper_fonts'):
        root._stepper_fonts = []
    for font_name, (fam, size, weight) in fonts.items():
        if font_name in tkfont.names(root):
            font = tkfont.nametofont(font_name, root=root)
        else:
            font = tkfont.Font(root=root, name=font_name, exists=False)
            root._stepper_fonts.append(font)
        font.configure(family=fam, size=round(size * text_scale), weight=weight)
    c = palette()
    style.configure('.', font='TkDefaultFont')
    style.configure('TButton', padding=(px(12), px(6)))
    style.configure('TEntry', padding=(px(8), px(5)))
    style.configure('TCombobox', padding=(px(8), px(5)))
    style.configure('TSpinbox', padding=(px(6), px(4)))
    style.configure('TNotebook.Tab', padding=(px(14), px(7)))
    style.configure('TLabelframe', padding=px(12))
    style.configure('TLabelframe.Label', font='StepperSection')
    style.configure('Treeview', rowheight=px(30))
    for bootstyle in ('success', 'danger', 'primary'):
        # Big call-to-action buttons (Start exposure, Stop).
        name = f'Big.{bootstyle}.TButton'
        style.configure(name, font='StepperBig', padding=(px(16), px(10)))
    style.configure('Muted.TLabel', foreground=c['muted'])
    style.configure('Small.TLabel', foreground=c['muted'], font='StepperSmall')
    style.configure('Caption.TLabel', foreground=c['muted'], font='StepperCaption')
    style.configure('Section.TLabel', font='StepperSection')
    style.configure('Title.TLabel', font='StepperTitle')
    for kind in ('ok', 'warn', 'bad', 'info', 'red', 'uv'):
        style.configure(f'{kind.capitalize()}.TLabel', foreground=c[kind])
    custom = ['Muted.TLabel', 'Small.TLabel', 'Caption.TLabel', 'Section.TLabel', 'Title.TLabel',
              'Big.success.TButton', 'Big.danger.TButton', 'Big.primary.TButton'] + \
             [f'{k.capitalize()}.TLabel' for k in ('ok', 'warn', 'bad', 'info', 'red', 'uv')]
    for name in custom:
        try:
            style._register_ttkstyle(name)  # so ttkbootstrap uses the name as-is
        except Exception:
            pass
    root.option_add('*TCombobox*Listbox.font', 'TkDefaultFont')
    root.option_add('*TCombobox*Listbox.background', c['inputbg'])
    root.option_add('*TCombobox*Listbox.foreground', c['fg'])
    try:
        root.configure(background=c['bg'])
    except Exception:
        pass
    return style


def prepare_dropdowns(root):
    """Give combobox popups readable padding and theme colours."""
    def visit(widget):
        if isinstance(widget, ttk.Combobox) and not getattr(widget, '_stepper_prepared', False):
            def prepare(w=widget, previous=widget.cget('postcommand')):
                if previous:
                    try:
                        w.tk.eval(previous)
                    except Exception:
                        pass
                try:
                    popup = w.tk.call('ttk::combobox::PopdownWindow', str(w))
                    c = palette()
                    w.tk.call(f'{popup}.f.l', 'configure', '-font', 'TkDefaultFont',
                              '-borderwidth', px(6), '-relief', 'flat', '-highlightthickness', 0,
                              '-background', c['inputbg'], '-foreground', c['fg'],
                              '-selectbackground', c['primary'], '-selectforeground', '#ffffff')
                except Exception:
                    pass
            widget.configure(postcommand=prepare)
            widget._stepper_prepared = True
        for child in widget.winfo_children():
            visit(child)
    visit(root)
