"""Separate Agent 3 sequence/test and read-only timing observer sources.

The three saved Agent 2 files are never edited. UVM_TESTNAME selects a derived
test; its build phase installs the sequence override before the inherited test
constructs/runs the original environment and checker lifecycle.
"""


def build_extension(item, controller, inventory):
    roles, widths = item['roles'], item['widths']
    assignments = '\n'.join('          req.{} = {};'.format(roles[role], variable) for role, variable in
        (('kind','operation'),('cycles','cycles'),('bank','bank_value'),('address','address_value'),
         ('data','data_value'),('write','write_value'),('scenario','vector_id')))
    package = '''`timescale 1ns/1ps
`include "uvm_macros.svh"
package agent3_system_pkg;
  import uvm_pkg::*;
  import dut_pkg::*;
  class agent3_system_sequence extends SEQUENCE_PARENT;
    `uvm_object_utils(agent3_system_sequence)
    function new(string name="agent3_system_sequence"); super.new(name); endfunction
    task body();
      ITEM_TYPE req;
      int fd, parsed, operation, cycles, write_value, index=0, elapsed;
      bit [BANK_WIDTH-1:0] bank_value;
      bit [ADDRESS_WIDTH-1:0] address_value;
      bit [DATA_WIDTH-1:0] data_value;
      string filename, vector_id;
      if (!$value$plusargs("AGENT3_VECTOR_FILE=%s",filename))
        `uvm_fatal("AGENT3_PLAN","Missing reproducible vector file")
      fd=$fopen(filename,"r");
      if (fd==0) `uvm_fatal("AGENT3_PLAN","Cannot open vector file")
      $display("AGENT3_RUNTIME_START|file=%s",filename);
      while (!$feof(fd)) begin
        parsed=$fscanf(fd,"%d %d %d %h %h %h %s\\n",operation,cycles,write_value,bank_value,address_value,data_value,vector_id);
        if (parsed==7) begin
          if (index==0) $display("AGENT3_BEGIN|vector=%s",vector_id);
          if (operation==4 || operation==5) begin
            elapsed=0;
            while (!((operation==4 && vif.mon.completion===1 && vif.mon.service_write===0) ||
                     (operation==5 && vif.mon.dispatch===1)) && elapsed<cycles) begin
              @(vif.mon); elapsed++;
            end
            if (elapsed>=cycles) $display("AGENT3_GATE_MISSED|vector=%s|gate=%s",vector_id,operation==4 ? "pending_read_response" : "active_service");
            else $display("AGENT3_GATE_HIT|vector=%s|gate=%s|cycle=%0d",vector_id,operation==4 ? "pending_read_response" : "active_service",vif.cycle);
          end else begin
            req=ITEM_TYPE::type_id::create("agent3_host_item");
            start_item(req);
ASSIGNMENTS
            if (operation==0)
              $display("AGENT3_ISSUE|vector=%s|step=%0d|cycle=%0d|bank=%0d|address=%h|write=%0d|data=%h",vector_id,index,vif.cycle,bank_value,address_value,write_value,data_value);
            finish_item(req);
          end
          index++;
        end else if (!$feof(fd)) `uvm_fatal("AGENT3_PLAN","Malformed vector record")
      end
      $fclose(fd);
      $display("AGENT3_END|vector=%s|steps=%0d",vector_id,index);
    endtask
  endclass
  class agent3_system_test extends dut_test;
    `uvm_component_utils(agent3_system_test)
    function new(string name,uvm_component parent); super.new(name,parent); endfunction
    function void build_phase(uvm_phase phase);
      SEQUENCE_PARENT::type_id::set_type_override(agent3_system_sequence::get_type());
      super.build_phase(phase);
    endfunction
  endclass
endpackage
'''
    for key, value in (('SEQUENCE_PARENT',item['sequence_parent']), ('ITEM_TYPE',item['item_type']),
                       ('BANK_WIDTH',widths['bank']),('ADDRESS_WIDTH',widths['address']),
                       ('DATA_WIDTH',widths['data']),('ASSIGNMENTS',assignments)):
        package = package.replace(key, str(value))
    declarations, logging, probes = [], [], []
    index = 0
    for rule in inventory:
        if rule['classification']!='APPLICABLE' or rule['kind']!='delay': continue
        for observer in rule['observers']:
            probes += [observer['start'],observer['end']]
            declarations += ['    input timer_{}_start = {};'.format(index,observer['start']),
                             '    input timer_{}_end = {};'.format(index,observer['end'])]
            for event in ('start','end'):
                logging += ['    if (timing.timer_{i}_{event}===1 && previous_{event}[{i}]!==1)'.format(i=index,event=event),
                    '      $display("AGENT3_TIMING_EVENT|event={event}|rule={rule}|channel={i}|bank={bank}|cycle=%0d|vector=%s",dut_tb_top.bus.cycle,dut_tb_top.bus.active_scenario);'.format(
                        event=event,rule=rule['rule'],i=index,bank=observer['bank'] if observer['bank'] is not None else -1)]
            logging += ['    previous_start[{i}]=timing.timer_{i}_start; previous_end[{i}]=timing.timer_{i}_end;'.format(i=index)]
            index += 1
    observer = '''`timescale 1ns/1ps
module agent3_system_bootstrap;
  import uvm_pkg::*;
  import agent3_system_pkg::*;
  // Static initialization registers the selected test before run_test.
  uvm_object_wrapper registration=agent3_system_test::get_type();
  bit previous_reset=0;
  bit [TIMER_COUNT-1:0] previous_start='0, previous_end='0;
WAVEFORM_PROBES
  clocking timing @(posedge dut_tb_top.clk);
    default input #1step;
DECLARATIONS
  endclocking
  initial forever begin
    @(dut_tb_top.bus.mon);
    #2ns; // Original monitor/scoreboard publishes its ledger at #1ns.
    if (dut_tb_top.bus.mon.rst_n!==1) begin
      if (!previous_reset) $display("AGENT3_TIMING_EVENT|event=reset|cycle=%0d|vector=%s",dut_tb_top.bus.cycle,dut_tb_top.bus.active_scenario);
      previous_reset=1; previous_start='0; previous_end='0;
    end else begin
      previous_reset=0;
      if (dut_tb_top.bus.mon.act===1)
        $display("AGENT3_TIMING_EVENT|event=act|bank=%0d|cycle=%0d|vector=%s",dut_tb_top.bus.mon.selected_bank,dut_tb_top.bus.cycle,dut_tb_top.bus.active_scenario);
      if (dut_tb_top.bus.mon.refresh===1)
        $display("AGENT3_TIMING_EVENT|event=refresh_sample|value=1|dispatch=%0d|cycle=%0d|vector=%s",dut_tb_top.bus.mon.dispatch,dut_tb_top.bus.cycle,dut_tb_top.bus.active_scenario);
      if (dut_tb_top.bus.mon.txn_valid===1 || dut_tb_top.bus.mon.dispatch===1 || dut_tb_top.bus.mon.completion===1 || dut_tb_top.bus.mon.rsp_valid===1 || dut_tb_top.bus.mon.refresh===1)
        $display("AGENT3_SAMPLE|vector=%s|cycle=%0d|valid=%0d|ready=%0d|dispatch=%0d|completion=%0d|response=%0d|write=%0d|row_hit=%0d|row_miss=%0d|row_closed=%0d|full=%0d|refresh=%0d|outstanding=%0d|service_pending=%0d",dut_tb_top.bus.active_scenario,dut_tb_top.bus.cycle,dut_tb_top.bus.mon.txn_valid,dut_tb_top.bus.mon.txn_ready,dut_tb_top.bus.mon.dispatch,dut_tb_top.bus.mon.completion,dut_tb_top.bus.mon.rsp_valid,dut_tb_top.bus.mon.txn_is_write,dut_tb_top.bus.mon.row_hit,dut_tb_top.bus.mon.row_miss,dut_tb_top.bus.mon.row_closed,dut_tb_top.bus.mon.queue_full,dut_tb_top.bus.mon.refresh,dut_tb_top.bus.outstanding,dut_tb_top.bus.mon.service_pending);
LOGGING
    end
  end
endmodule
'''.replace('TIMER_COUNT',str(max(1,index))).replace('DECLARATIONS','\n'.join(declarations)).replace('LOGGING','\n'.join(logging))
    observer=observer.replace('WAVEFORM_PROBES', '  initial begin #1ps; $dumpvars(0, '+', '.join(probes)+'); end' if probes else '')
    # The clocking block always has an input, even with no bank timer probes.
    observer = observer.replace('default input #1step;', 'default input #1step;\n    input reset_n = dut_tb_top.bus.rst_n;')
    return {'agent3_system_pkg.sv':package, 'agent3_system_bootstrap.sv':observer}
