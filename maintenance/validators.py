"""Custom validators for cross-model integrity rules."""
from django.core.exceptions import ValidationError


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
            "component": "Component requires a machine to be set."
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
        valid_names = ", ".join(c.name for c in valid[:5]) or "(none)"
        raise ValidationError({
            "component": (
                f"Component '{component.name}' does not belong to machine '{machine.name}'. "
                f"Valid components for this machine: {valid_names}"
                f"{' ...' if len(valid) > 5 else ''}"
            )
        })
