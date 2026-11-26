from __future__ import annotations

from app import cli_nav


def drive(inputs, handlers=None):
    events = []
    stream = iter(inputs)
    if handlers is None:
        handlers = {name: (lambda name=name: events.append(("run", name)))
                    for name in cli_nav.action_names()}

    def read(_menu):
        try:
            return next(stream)
        except StopIteration:
            raise EOFError

    code = cli_nav.run_menus(
        cli_nav.MENUS, handlers,
        show=lambda menu, stack: events.append(("show", tuple(stack))),
        read_choice=read,
        invalid=lambda raw: events.append(("invalid", raw)),
        announce=lambda item: events.append(("announce", item.label)),
        pause=lambda: events.append(("pause",)),
        interrupted=lambda: events.append(("interrupted",)),
    )
    return code, events


def test_menu_actions_have_unique_keys_and_valid_submenus():
    for name, menu in cli_nav.MENUS.items():
        keys = [item.key for item in menu.items]
        assert len(keys) == len(set(keys))
        for item in menu.items:
            if item.kind == cli_nav.SUBMENU:
                assert item.target in cli_nav.MENUS
        if name != "main":
            assert menu.parent == "main"
            assert menu.items[-1].kind == cli_nav.BACK


def test_all_menu_actions_are_wired_in_cli():
    from app import cli
    # Static source-level contract: each navigation target must be represented in the
    # interactive handler map. The UI itself is tested independently of systemd.
    source = (cli.__file__ and __import__("pathlib").Path(cli.__file__).read_text())
    for action in cli_nav.action_names():
        assert f'"{action}"' in source


def test_submenu_navigation_and_back():
    code, events = drive(["2", "0", "q"])
    assert code == 0
    assert [event[1] for event in events if event[0] == "show"] == [
        ("main",), ("main", "bot"), ("main",)
    ]


def test_logs_follow_action_runs_from_logs_submenu():
    _, events = drive(["9", "2", "0", "q"])
    assert [event[1] for event in events if event[0] == "run"] == ["logs_follow"]


def test_invalid_input_does_not_run_action():
    _, events = drive(["1;whoami", "q"])
    assert not [event for event in events if event[0] == "run"]
    assert any(event[0] == "invalid" for event in events)


def test_uninstall_is_only_in_bot_submenu_and_flagged_dangerous():
    main_targets = {item.target for item in cli_nav.MENUS["main"].items}
    assert "uninstall" not in main_targets
    entries = [item for menu in cli_nav.MENUS.values() for item in menu.items
               if item.target == "uninstall"]
    assert len(entries) == 1 and entries[0].danger


def test_managed_bot_observer_is_registered_on_root_dispatcher():
    from pathlib import Path
    import ast
    source = (Path(__file__).resolve().parents[1] / "app" / "handlers" / "tenants.py").read_text()
    tree = ast.parse(source)
    decorators = [
        ast.unparse(node.decorator_list[0])
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "managed_bot_updated"
    ]
    # dispatcher.update(...) never fires (the Dispatcher's own catch-all update handler runs
    # first and stops propagation); the typed managed_bot observer is the one aiogram calls.
    # The behaviour is proven end to end in tests/test_managed_bot_registration.py.
    assert decorators == ["router.managed_bot()"]
    assert "@dispatcher.update" not in source
