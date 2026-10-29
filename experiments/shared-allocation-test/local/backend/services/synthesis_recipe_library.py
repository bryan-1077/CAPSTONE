from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SynthesisRecipe:
    recipe_id: str
    recipe_class: str
    description: str
    match_conditions: list[str]
    confidence_rules: list[str]
    generated_file_targets: list[str]
    metadata_updates: list[str]
    reviewer_validation_checks: list[str]


_RECIPES: tuple[SynthesisRecipe, ...] = (
    SynthesisRecipe(
        recipe_id="generic_interface_top_to_impl",
        recipe_class="generic_interface_top_to_impl",
        description=(
            "Generate a build-only *_impl copy of the selected synthesis top and "
            "rewrite raw generic interface ports to inferred typed modports."
        ),
        match_conditions=[
            "Selected top exposes raw generic interface ports.",
            "Typed modport mapping for every raw port is high-confidence and unambiguous.",
        ],
        confidence_rules=[
            "Require consistent evidence from top instantiations or explicit mapping hints.",
            "Do not guess when multiple mappings are observed for the same port.",
        ],
        generated_file_targets=["generated/<Top>_impl.sv"],
        metadata_updates=["top_module.txt", "filelist_genus.f", "README_prepared.txt"],
        reviewer_validation_checks=[
            "Generated *_impl top exists.",
            "New top no longer exposes raw generic interface ports.",
            "Original top source remains unchanged in the prepared build.",
        ],
    ),
    SynthesisRecipe(
        recipe_id="interface_wrapper_to_flat_ports",
        recipe_class="interface_wrapper_to_flat_ports",
        description=(
            "Replace a known interface-only wrapper with a build-only flattened port wrapper."
        ),
        match_conditions=[
            "Wrapper behavior is a simple projection of interface fields to local ports.",
            "All interface field widths and directions are known from trusted project evidence.",
        ],
        confidence_rules=[
            "Only apply to wrappers with a reviewed deterministic template.",
        ],
        generated_file_targets=["generated/<Module>_impl.sv"],
        metadata_updates=["filelist_genus.f", "README_prepared.txt"],
        reviewer_validation_checks=[
            "Generated wrapper compiles with the prepared bundle.",
        ],
    ),
    SynthesisRecipe(
        recipe_id="ddr_behavioral_readwrite_to_clock2x_abstraction",
        recipe_class="ddr_behavioral_readwrite_to_clock2x_abstraction",
        description=(
            "Replace known DDR behavioral DQS-driven transport logic with a build-only "
            "clock2x-based synthesis abstraction."
        ),
        match_conditions=[
            "Known ReadWrite-style module uses DQS pins as behavioral clocks or multi-edge event controls.",
            "An approved synthesis abstraction template exists for the project class.",
        ],
        confidence_rules=[
            "Only apply when the affected module set matches a registered recipe.",
        ],
        generated_file_targets=["generated/ReadWrite_synth.sv"],
        metadata_updates=["filelist_genus.f", "README_prepared.txt"],
        reviewer_validation_checks=[
            "Original unsynthesizable ReadWrite file is excluded from the synthesis filelist.",
            "Generated abstraction file is present in the filelist.",
        ],
    ),
    SynthesisRecipe(
        recipe_id="tri_state_bus_model_to_synth_abstraction",
        recipe_class="tri_state_bus_model_to_synth_abstraction",
        description=(
            "Replace a known tri-state-heavy behavioral bus model with an approved "
            "synthesis-safe abstraction in the prepared build."
        ),
        match_conditions=[
            "Module is a recognized behavioral bus model and an approved abstraction exists.",
        ],
        confidence_rules=[
            "Do not apply to unknown tri-state logic without an approved recipe.",
        ],
        generated_file_targets=["generated/<Module>_synth.sv"],
        metadata_updates=["filelist_genus.f", "README_prepared.txt"],
        reviewer_validation_checks=[
            "Generated abstraction replaces the original only in the prepared build.",
        ],
    ),
    SynthesisRecipe(
        recipe_id="communication_wrapper_to_flat_transaction_ports",
        recipe_class="communication_wrapper_to_flat_transaction_ports",
        description=(
            "Generate build-only flattened communication wrappers that expose explicit "
            "transaction/data ports instead of interface handles."
        ),
        match_conditions=[
            "Wrapper is a known communication adapter with deterministic signal mapping.",
        ],
        confidence_rules=[
            "Only apply when the wrapper matches a reviewed project recipe or explicit mapping.",
        ],
        generated_file_targets=[
            "generated/receive_command_impl.sv",
            "generated/send_read_data_impl.sv",
        ],
        metadata_updates=["filelist_genus.f", "README_prepared.txt"],
        reviewer_validation_checks=[
            "Generated communication wrappers are used by the generated synthesis top.",
        ],
    ),
    SynthesisRecipe(
        recipe_id="memory_controller_impl_bundle",
        recipe_class="project_impl_bundle",
        description=(
            "Project-class MemoryController adaptation bundle that generates "
            "MemoryController_impl, MemContFSM_impl, flattened communication wrappers, "
            "and a synthesis-safe ReadWrite replacement."
        ),
        match_conditions=[
            "Selected top is MemoryController.",
            "Prepared bundle contains Controller_FSM.sv, Receive_command.sv, Read_data_comm.sv, and ReadWrite.sv.",
            "Detected raw generic interface top and DDR behavioral ReadWrite patterns.",
        ],
        confidence_rules=[
            "Prefer this recipe over generic top-only adaptation when all expected source files are present.",
            "Fail safely if any required source file is missing.",
        ],
        generated_file_targets=[
            "generated/MemoryController_impl.sv",
            "generated/MemContFSM_impl.sv",
            "generated/receive_command_impl.sv",
            "generated/send_read_data_impl.sv",
            "generated/ReadWrite_synth.sv",
        ],
        metadata_updates=["top_module.txt", "filelist_genus.f", "README_prepared.txt"],
        reviewer_validation_checks=[
            "top_module.txt points to MemoryController_impl.",
            "Original interface-heavy source files are preserved but excluded from the synthesis filelist.",
            "Generated *_impl files are present in the prepared build.",
        ],
    ),
)


def build_synthesis_recipe_library_payload() -> dict[str, Any]:
    return {
        "recipes": [asdict(item) for item in _RECIPES],
        "hint_filenames": [
            "synthesis_adaptation_hints.json",
            "synthesis_mapping_hints.json",
        ],
        "default_mapping_hints": {
            "interface_port_mappings": {
                "MemoryController": {
                    "DDR4Bus": "DDR4Interface.Controller",
                    "MCTx": "C2MInterface.Mem_data",
                    "MCRx": "C2MInterface.Mem_tran",
                }
            },
            "interface_type_port_mappings": {},
            "interface_type_default_modports": {},
            "mapping_hint_schema": {
                "interface_port_mappings": {
                    "<TopModule>": {
                        "<port_name>": "<Interface>.<Modport> or <Modport>",
                        "*": "<Interface>.<Modport> or <Modport>",
                    },
                    "*": {
                        "<port_name>": "<Interface>.<Modport> or <Modport>",
                    },
                },
                "interface_type_port_mappings": {
                    "<InterfaceType>": {
                        "<TopModule>.<port_name>": "<Interface>.<Modport> or <Modport>",
                        "<port_name>": "<Interface>.<Modport> or <Modport>",
                        "*": "<Interface>.<Modport> or <Modport>",
                    }
                },
                "interface_type_default_modports": {
                    "<InterfaceType>": "<Interface>.<Modport> or <Modport>",
                },
            },
            "preferred_recipe_by_top": {
                "MemoryController": "memory_controller_impl_bundle",
            },
            "project_recipe_requirements": {
                "memory_controller_impl_bundle": {
                    "top_modules": ["MemoryController"],
                    "required_files": [
                        "rtl/MemoryController.sv",
                        "rtl/Controller_FSM.sv",
                        "rtl/Receive_command.sv",
                        "rtl/Read_data_comm.sv",
                        "rtl/ReadWrite.sv",
                        "rtl/DDR4Interface.sv",
                        "rtl/C2MInterface.sv",
                    ],
                }
            },
        },
    }
