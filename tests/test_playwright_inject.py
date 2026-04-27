from tools.playwright.inject_style import get_style


def test_style_contains_colors():
    s = get_style()
    assert 'e6ffea' in s.lower()
    assert '006622' in s.replace('#','').lower() or '006600' in s.replace('#','').lower()
    assert 'background' in s
