"""The first campaign: executable DDR guard cases and explicitly planned primitives."""
from architecture import library_context

VERSION = 2
GUARD_CHECKS = ['reset_and_invalid', 'tRCD_per_bank', 'tRAS_per_bank', 'tRRD_global',
                'tFAW_window', 'probes_no_history', 'reset_history', 'long_idle', 'seeded_history']


def cases():
    library = library_context()
    result = []
    for profile in ('DDR4-2400', 'DDR4-3200'):
        cycles = {k: library['profiles'][profile]['timing_cycles'][k] for k in ('tRCD','tRAS','tRRD','tFAW')}
        name = 'timing_guard_' + profile.lower().replace('-', '_')
        text = (
            f'Design a standalone four-bank timing permission guard using {profile}. '
            'Use one synchronous rising-edge clk and synchronous active-low rst_n. '
            'For this baseline use exactly one datapath module contract with timing_enforcement role, '
            'the specified interface and no child modules or additional ports. '
            'No host, queue, scheduler, memory, refresh, or command sequencer is requested. '
            'Inputs: candidate_valid (1), candidate_command (45), commit_valid (1), commit_command (45), '
            'open_valid (4), open_rows (16). Output: candidate_ok (1). '
            'In the specification package use shared type names opcode, bank, row, column, data, command, bank_rows and bank_valid. '
            'command is a packed record with fields opcode, bank, row, column and wdata. '
            'bank_rows and bank_valid are four-element arrays of row and bit, respectively, '
            'using ascending_index_from_lsb packing. '
            'The packed command is opcode[44:42], bank[41:40], row[39:36], column[35:32], wdata[31:0]. '
            'NOP=0, ACT=1, PRE=2, READ=3, WRITE=4; 5..7 are reserved. '
            'Open rows use bank 0 in bits [3:0] through bank 3 in bits [15:12]. '
            'Expose BANK_COUNT=4, ROW_WIDTH=4, DATA_WIDTH=32, TFAW_ACT_LIMIT=4 in shared parameters. '
            'In shared.timing_profile use local timing_cycles binding paths for tRCD/tRAS/tRRD/tFAW, never numeric binding values. '
            'These paths and shared type names are specification/elaboration requirements. RTL may use the compiler-resolved '
            'numeric constants and equivalent packed vectors or structs; matching SystemVerilog typedef names or a '
            'runtime YAML lookup are not required. Preserve the exact public widths, packed layout and behavior. '
            'Permission uses pre-edge committed history: ACT requires a closed bank, tRRD from all prior ACTs '
            'and fewer than four prior ACTs aged strictly less than tFAW. PRE requires an open bank and '
            'tRAS since its latest ACT. READ/WRITE require a matching open row and tRCD since its latest ACT. '
            'An absent timing history imposes no synthetic reset delay. Reset gates permission low and clears '
            'all history. Only commit_valid ACT updates histories; probes, invalid cycles and other opcodes '
            'do not create ACT history. candidate_ok is zero for an invalid candidate, NOP or reserved opcode. '
            'A trigger at E stops blocking at E+T; history aged exactly tFAW is excluded. Simultaneous '
            'permission and commit are evaluated from old history, then the new commit updates future history. '
            'Do not infer bank state internally: declared open_valid/open_rows are authoritative inputs. '
            'Use timing_enforcement roles and declared port observations for isolated verification.')
        result.append({'id':name,'family':'timing_guard','checker_status':'executable',
                       'checker_profile':'ddr4-timing-guard-v1','profile':profile,
                       'expected_cycles':cycles,'required_checks':list(GUARD_CHECKS),
                       'config':{'design_name':name,'design_type':'generic','profile':profile,'requirements':[text]}})
    for value in (1,2,31,32,33):
        name = f'reloadable_timer_t{value}'
        text = (f'Design a standalone reloadable timer with T_CYCLES={value}. '
                'Inputs clk, rst_n, start and enable are one bit; outputs busy and done are one bit. '
                'Use synchronous active-low reset. Reset has priority over start, enable and expiration. '
                'A start at edge E starts/restarts the delay, clears done and sets busy after E. '
                'Without another start, consume one count on each later enable edge; after exactly T_CYCLES '
                'enabled edges, clear busy and assert done for one cycle. Disabled edges retain remaining '
                'time. start wins over simultaneous expiration. done clears on the following edge even '
                'when enable is low. A new start while busy restarts the full interval. Reset gates busy/done '
                'low and cancels pending work. Use timing_enforcement role, accurate dependency declarations, '
                'and explicit admission/start/expiration observations. No DDR controller or refresh is requested.')
        result.append({'id':name,'family':'reloadable_timer','checker_status':'planned',
                       'checker_profile':None,'required_checks':['enabled_edge_count','restart_priority',
                       'reset_cancel','pause_resume','done_pulse','counter_width'],
                       'config':{'design_name':name,'design_type':'generic','parameters':{'T_CYCLES':value},
                                 'requirements':[text]}})
    return result
