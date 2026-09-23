"""Identity domain package."""

from .service import (
    decay_themes,
    get_identity,
    get_persona_db_baseline,
    get_persona,
    get_persona_status,
    get_themes,
    is_persona_initialized,
    render_persona,
    seed_persona,
    set_persona_baseline,
    update_narrative,
    update_persona,
    update_themes,
)
from .examples import (
    add_example,
    delete_example,
    list_examples,
    render_examples,
    restore_example,
    retire_example,
)
from .curiosity import (
    add_curiosity,
    address_curiosity,
    dismiss_curiosity,
    get_pending_curiosities,
    render_curiosity_prompt,
)
from .reflection import apply_reflection, prepare_reflection_context, run_reflection
from .evidence import IdentityEvidence, get_self_reflection_evidence
from .personality import (
    PERSONALITY_MODULES,
    clear_situational_humor_state,
    evaluate_situational_humor,
)

