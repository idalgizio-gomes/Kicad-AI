"""
Write chat tool wrapping the sibling "Coil Generators" plugin (by
SK-Electronics-Consulting), installed via KiCad's own Plugin and Content
Manager (PCM) — like ``kicad_parasitics_tools.py``, reached through
``_sibling_plugin.py``'s ``find_pcm_plugin_dir()`` since a PCM install has no
nested ``plugins/`` subfolder.

Unlike every other sibling plugin wrapped so far, this one is built on
KiCad's OWN documented, stable "Footprint Wizard" API
(``pcbnew.FootprintWizardPlugin`` via the ``FootprintWizardBase`` module
KiCad itself ships at
``share/kicad/scripting/plugins/FootprintWizardBase.py``) — a real,
first-party mechanism for parametric footprint generation, not a bespoke
GUI-tangled plugin. ``coil_generator.py`` defines two wizard classes
(``CoilGeneratorID2L``, ``CoilGenerator1L1T``) and
``flux_neutral_coil_generator.py`` a third (``FluxNeutralCoilGen``) — all
three subclass ``FootprintWizardBase.FootprintWizard`` and only implement
``GenerateParameterList``/``CheckParameters``/``BuildThisFootprint``, never
touching wx at all.

VERIFIED END-TO-END (executed directly against KiCad 10.0.4's own embedded
Python + the real installed plugin today, not guessed):

- ``wizard.parameters`` is a plain ``dict[page][name] -> value`` (e.g.
  ``{"Install Info": {"Inside Diameter, Radius": 30000000, ...}}``) but its
  values are ALREADY in KiCad's internal units (nanometers) for ``mm``-typed
  parameters — confirmed ``30`` (the default, in mm) round-trips to
  ``30000000`` internally. Mutating this dict directly is NOT the safe path.
- The SAFE way to change a parameter: ``wizard.params`` is a parallel list of
  real ``pcbnew.FootprintWizardParameter`` objects, each with ``.page``,
  ``.name``, ``.units`` ("mm"/"integer"/"bool"/"string"), and a
  ``.SetValue(str)`` method — confirmed by reading its real source
  (``FootprintWizardParameters``, bundled inside ``FootprintWizardBase``'s
  own import) that ``SetValue`` takes a HUMAN-readable value (e.g. the
  string "20" for 20mm, "15" for an integer count, "true"/"false" for a
  bool) and handles unit conversion internally via its own ``.value``
  property — calling ``pcbnew.FromMM()` manually here would be WRONG
  (double conversion). This module always looks up the parameter object by
  (page, name) in ``wizard.params`` and calls ``.SetValue(str(given_value))``,
  never touching ``wizard.parameters`` directly.
- ``wizard.BuildFootprint()`` (defined on the KiCad-provided base class,
  "do not override") calls ``CheckParameters()`` then, if
  ``wizard.AnyErrors()`` is False, ``BuildThisFootprint()`` and leaves the
  resulting ``pcbnew.FOOTPRINT`` on ``wizard.module``. If there ARE errors,
  ``BuildFootprint()`` returns without raising — this module checks
  ``wizard.AnyErrors()`` explicitly afterward and raises RuntimeError with
  ``wizard.buildmessages`` (KiCad's own human-readable parameter-error text)
  if so.
- Confirmed by an actual board.Add() + SetReference() + SetPosition() call
  against a real ``CoilGeneratorID2L`` instance: the resulting ``.module``
  behaves exactly like any other footprint for placement purposes — same
  pattern already used by ``add_footprint``/``coil_creator_tools.py``.

Each wizard class's own parameter set (page/name/units/default), confirmed
by reading ``coil_generator.py``/``flux_neutral_coil_generator.py`` directly:

- "dual_layer_id" (``CoilGeneratorID2L``): Coil specs: Total Turns (int),
  First Layer / Second Layer (layer name strings, "_" not "."), Direction
  (bool). Install Info: Inside Diameter, Radius (mm), Inner Ring gap (mm).
  Fab Specs: Trace Width, Trace Spacing, Via Drill, Via Annular Ring, Pad
  Drill, Pad Annular Ring (all mm).
- "single_layer_1turn" (``CoilGenerator1L1T``): Coil specs: Stub Length
  (mm), Layer (string), Direction (bool). Install Info: Radius (mm). Fab
  Specs: Trace Width, Trace Spacing, Pad Drill, Pad Annular Ring (mm).
- "flux_neutral" (``FluxNeutralCoilGen``): Coil specs: Turns (int), Minimum
  Radius (mm), Stub Length (mm), plus two more Coil-specs params and
  Install-Info/Fab-Specs params mirroring the other two wizards (read the
  file directly for the exact remaining names before relying on them from
  outside this module — this docstring lists only what was confirmed by a
  quick grep, not a full line-by-line read, unlike the other two classes).

This tool accepts a generic ``parameters: {page: {name: value}}`` dict
(rather than one bespoke named argument per wizard field) — deliberately
simpler than the fully-spelled-out style used by ``coil_creator_tools.py``,
given three distinct wizards with ~10 fields apiece; every field not
supplied keeps the wizard's own real default (never invented here). Unknown
(page, name) pairs are reported back listing the real available ones,
never silently ignored.
"""

from __future__ import annotations

try:
    from .framework import ActionDefinition, ActionRegistry
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions.framework import ActionDefinition, ActionRegistry

try:
    from ..llm_providers.base import ToolSpec
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from llm_providers.base import ToolSpec

try:
    from ._sibling_plugin import (
        SiblingPluginNotFoundError,
        find_pcm_plugin_dir,
        load_sibling_module,
    )
except ImportError:  # pragma: no cover - fallback for flat/test imports
    from actions._sibling_plugin import (  # type: ignore[no-redef]
        SiblingPluginNotFoundError,
        find_pcm_plugin_dir,
        load_sibling_module,
    )

try:
    from .. import i18n as _i18n
except ImportError:  # pragma: no cover - fallback for flat/test imports
    import i18n as _i18n  # type: ignore[no-redef]


def _(message: str) -> str:  # noqa: N807 - conventional gettext alias name
    return _i18n._(message)


_PACKAGE_NAME = "_sibling_coil_generators"

_SIBLING_IDENTIFIER = "com_github_SK-Electronics-Consulting_kicad-coil-generators"

_WIZARD_MODULES = {
    "dual_layer_id": ("coil_generator", "CoilGeneratorID2L"),
    "single_layer_1turn": ("coil_generator", "CoilGenerator1L1T"),
    "flux_neutral": ("flux_neutral_coil_generator", "FluxNeutralCoilGen"),
}


def _load(submodule: str):
    plugins_dir = find_pcm_plugin_dir(_SIBLING_IDENTIFIER)
    return load_sibling_module(_PACKAGE_NAME, plugins_dir, submodule)


def _not_installed_message() -> str:
    return _(
        "O plugin Coil Generators não está instalado nesta máquina — esta "
        "ferramenta precisa dele."
    )


def _get_board():
    """Lazily import pcbnew and return the currently open board. Mirrors
    kicad_tools.py's _get_board()."""
    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            _("pcbnew indisponível — esta ferramenta só funciona dentro do KiCad")
        ) from exc

    board = pcbnew.GetBoard()
    if board is None:
        raise RuntimeError(_("Nenhum board KiCad está atualmente aberto"))
    return pcbnew, board


def _describe_params(wizard) -> str:
    lines = []
    for p in wizard.params:
        lines.append(f"  [{p.page}] {p.name} ({p.units}, default={p.default})")
    return "\n".join(lines)


def generate_coil_wizard_footprint(args: dict) -> str:
    """Generate a PCB coil footprint via KiCad's own Footprint Wizard API,
    using one of the sibling Coil Generators plugin's three real wizards,
    and add it to the currently open board.

    Required args:
        coil_type: str — one of "dual_layer_id" (dual-layer coil sized to
            fit a circular aperture), "single_layer_1turn" (single-layer,
            single-turn loop), "flux_neutral" (multi-turn, flux-cancelling
            winding). Each has its own distinct real parameter set — see
            this module's docstring, or call this tool once with an
            unsupported coil_type to get a RuntimeError listing the valid
            ones.
        reference: str — reference designator for the new footprint (e.g.
            'L1'); must not already exist on the board.
        x_mm, y_mm: number — placement position, mm.

    Optional args:
        rotation_deg: number, default 0.
        parameters: object — {page: {name: value}} overrides for any of
            the wizard's own real parameters (see docstring for each
            coil_type's real page/name/units). Fields not given keep the
            wizard's own default. An unknown (page, name) pair raises
            RuntimeError listing every real parameter for that coil_type.
    """
    args = args or {}
    coil_type = args.get("coil_type")
    if coil_type not in _WIZARD_MODULES:
        raise RuntimeError(
            _(
                "Argumento 'coil_type' inválido ou em falta: '{value}' "
                "(use um de: {valid})."
            ).format(value=coil_type, valid=", ".join(_WIZARD_MODULES))
        )
    reference = args.get("reference")
    if not reference:
        raise RuntimeError(_("Falta o argumento 'reference'."))
    try:
        x_mm = float(args["x_mm"])
        y_mm = float(args["y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumentos 'x_mm'/'y_mm' inválidos: {err}").format(err=exc)
        ) from exc
    try:
        rotation_deg = float(args.get("rotation_deg") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            _("Argumento 'rotation_deg' inválido: {err}").format(err=exc)
        ) from exc

    overrides = args.get("parameters") or {}
    if not isinstance(overrides, dict):
        raise RuntimeError(_("Argumento 'parameters' deve ser um objeto {página: {nome: valor}}."))

    pcbnew, board = _get_board()
    if board.FindFootprintByReference(reference) is not None:
        raise RuntimeError(
            _("Já existe um componente com a referência '{reference}' na placa.").format(
                reference=reference
            )
        )

    submodule, class_name = _WIZARD_MODULES[coil_type]
    try:
        mod = _load(submodule)
    except SiblingPluginNotFoundError:
        return _not_installed_message()
    except ImportError as exc:
        return _("Erro ao carregar o Coil Generators: {err}").format(err=exc)

    wizard = getattr(mod, class_name)()

    for page, fields in overrides.items():
        if not isinstance(fields, dict):
            raise RuntimeError(
                _("'parameters[{page}]' deve ser um objeto {{nome: valor}}.").format(
                    page=page
                )
            )
        for name, value in fields.items():
            matches = [p for p in wizard.params if p.page == page and p.name == name]
            if not matches:
                raise RuntimeError(
                    _(
                        "Parâmetro desconhecido ('{page}', '{name}') para "
                        "coil_type='{coil_type}'. Parâmetros disponíveis:\n{available}"
                    ).format(
                        page=page,
                        name=name,
                        coil_type=coil_type,
                        available=_describe_params(wizard),
                    )
                )
            matches[0].SetValue(str(value))

    wizard.BuildFootprint()
    if wizard.AnyErrors():
        raise RuntimeError(
            _("Erro ao gerar a bobina ({coil_type}): {messages}").format(
                coil_type=coil_type, messages=wizard.buildmessages
            )
        )

    footprint = wizard.module
    board.Add(footprint)
    footprint.SetReference(reference)
    footprint.SetPosition(
        pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm))
    )
    footprint.SetOrientationDegrees(rotation_deg)
    try:
        pcbnew.Refresh()
    except Exception:
        pass

    return _(
        "Bobina '{coil_type}' criada como {reference} em ({x:.3f}, {y:.3f}) "
        "mm. Guarde a placa (Ctrl+S) para persistir a alteração."
    ).format(coil_type=coil_type, reference=reference, x=x_mm, y=y_mm)


def register_coil_generators_tools(registry: ActionRegistry) -> None:
    """Register the Coil Generators-backed tool on the given ActionRegistry.

    Safe to call even when the sibling plugin isn't installed — the handler
    reports that honestly at call time (see _not_installed_message()),
    matching every other sibling-plugin wrapper in this package.
    """
    registry.register(
        ActionDefinition(
            spec=ToolSpec(
                name="generate_coil_wizard_footprint",
                description=(
                    "Call this to generate a PCB spiral/loop coil footprint "
                    "(wireless charging, RFID, antenna, small transformer "
                    "winding) using KiCad's own Footprint Wizard API via the "
                    "sibling Coil Generators plugin, and place it on the "
                    "currently open board. Three real coil types available: "
                    "'dual_layer_id' (fits around a circular aperture, two "
                    "copper layers), 'single_layer_1turn' (single loop, one "
                    "layer), 'flux_neutral' (multi-turn, flux-cancelling). "
                    "This MODIFIES the board and requires explicit user "
                    "approval."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "coil_type": {
                            "type": "string",
                            "description": (
                                "One of: dual_layer_id, single_layer_1turn, "
                                "flux_neutral."
                            ),
                        },
                        "reference": {
                            "type": "string",
                            "description": "Reference designator, e.g. 'L1'.",
                        },
                        "x_mm": {"type": "number", "description": "Placement X, mm."},
                        "y_mm": {"type": "number", "description": "Placement Y, mm."},
                        "rotation_deg": {
                            "type": "number",
                            "description": "Rotation angle in degrees (default 0).",
                        },
                        "parameters": {
                            "type": "object",
                            "description": (
                                "{page: {name: value}} overrides for any of "
                                "the chosen wizard's real parameters (see "
                                "tool description / module docs for each "
                                "coil_type's real fields). Omitted fields "
                                "keep the wizard's own default."
                            ),
                        },
                    },
                    "required": ["coil_type", "reference", "x_mm", "y_mm"],
                },
            ),
            handler=generate_coil_wizard_footprint,
            read_only=False,
        )
    )
