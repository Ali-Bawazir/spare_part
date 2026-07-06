"""Custom validators for cross-model integrity rules."""
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


def validate_component_belongs_to_machine(component, machine):
    """
    Assert that a component (level-5 Machine) belongs to the specified machine (level-3).

    Walks the component's parent chain. If the level-3 ancestor is not the given machine,
    raise ValidationError. If component is None, no-op.
    """
    if component is None:
        return

    if machine is None:
        raise ValidationError({
            "component": _("Component requires a machine to be set.")
        })

    current = component.parent
    root_machine = None
    while current is not None:
        if current.asset_level == 3:
            root_machine = current
            break
        current = current.parent

    if root_machine is None or root_machine.pk != machine.pk:
        valid = machine.get_descendant_components() if machine else []
        valid_names = ", ".join(c.name for c in valid[:5]) or _("(none)")
        raise ValidationError({
            "component": (
                _("Component '%(component)s' does not belong to machine '%(machine)s'. "
                  "Valid components for this machine: %(valid)s%(more)s")
                % {
                    "component": component.name,
                    "machine": machine.name,
                    "valid": valid_names,
                    "more": _(" ...") if len(valid) > 5 else "",
                }
            )
        })
