"""Behavioral regression against generated RTL; requires Verilator, no LLM calls.

Run: python3 -m unittest discover -s local/frontend -p test_request_queue.py -v
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import generate_wrapper as wrapper

ROOT = Path(__file__).resolve().parent


def simulate(test, directory, sources, bench):
    tb = directory / 'tb.sv'
    tb.write_text(bench)
    result = subprocess.run(
        ['verilator', '--binary', '--timing', '-j', '2', '-Wno-fatal',
         '--top-module', 'tb', '--Mdir', str(directory / 'obj'),
         *map(str, sources), str(tb)], capture_output=True, text=True, timeout=180)
    test.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr)
    result = subprocess.run([str(directory / 'obj/Vtb')], cwd=directory,
                            capture_output=True, text=True, timeout=30)
    test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    test.assertIn('PHASE2 PASS', result.stdout)


@unittest.skipUnless(shutil.which('verilator'), 'Verilator required')
class RequestQueueTests(unittest.TestCase):
    def test_indexed_queue_storage_and_slot_reuse(self):
        # Fill, reject when full, reuse a removed slot, preserve all 51 bits,
        # and reset while populated. Scoreboard compares every valid slot.
        bench = r'''
module tb;
    logic clk=0, rst_n=0, enq_valid=0, deq_en=0;
    always #5 clk=~clk;
    logic [50:0] enq_req=0;
    logic enq_ready;
    logic [203:0] req_array;
    logic [3:0] req_valid;
    logic [1:0] sel_idx=0;
    logic [50:0] expected[4];
    bit valid[4];
    int serial=0;
    ddr4_request_queue dut(.*);
    task automatic step(input bit push, input bit pop, input int idx);
        int slot;
        bit ready_before;
        @(negedge clk);
        enq_valid=push; deq_en=pop; sel_idx=2'(idx);
        serial++;
        enq_req={2'(serial),10'(serial*31),6'(serial*7),1'(serial),32'(serial*1234567)};
        #1;
        slot=-1;
        for(int i=0;i<4;i++) if(!valid[i] && slot<0) slot=i;
        ready_before=(slot>=0)||pop;
        if(enq_ready !== ready_before) $fatal(1,"Queue readiness mismatch");
        if(pop) valid[idx]=0;
        if(push && ready_before) begin
            if(slot<0) slot=idx;
            expected[slot]=enq_req; valid[slot]=1;
        end
        @(posedge clk); #1;
        for(int i=0;i<4;i++) begin
            if(req_valid[i] !== valid[i]) $fatal(1,"Valid mismatch slot %0d",i);
            if(valid[i] && req_array[i*51+:51] !== expected[i])
                $fatal(1,"Payload corrupted slot %0d",i);
        end
    endtask
    initial begin
        repeat(2) @(negedge clk);
        rst_n=1;
        for(int i=0;i<4;i++) step(1,0,0);
        repeat(20) step(1,0,0);
        step(1,1,2); // full queue, replace selected entry
        step(0,1,0);
        step(1,1,3); // free slot differs from dequeue slot
        for(int i=0;i<200;i++) begin
            int idx;
            idx=i%4;
            step((i%3)!=0,valid[idx] && (i%2==0),idx);
        end
        @(negedge clk); rst_n=0; enq_valid=0; deq_en=0;
        @(posedge clk); #1;
        if(req_valid !== 0) $fatal(1,"Reset did not empty queue");
        $display("PHASE2 PASS"); $finish;
    end
endmodule
'''
        with tempfile.TemporaryDirectory() as tmp:
            simulate(self, Path(tmp), [ROOT/'rtl_output/ddr4_request_queue.sv'], bench)

    def test_host_queue_handshake_and_completion(self):
        # Exercise freshly built wrappers for every supported bank count.
        for banks, policy in ((1, "open_page"), (2, "open_page"), (4, "open_page"), (4, "close_page")):
            with self.subTest(banks=banks, policy=policy), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                modules = wrapper.discover_available_modules()
                with patch.object(wrapper, 'load_page_policy', return_value=policy):
                    rtl = wrapper.build_phase1_row_buffer_wrapper_rtl(modules, banks)
                top = directory/'ddr4_controller_top.sv'
                top.write_text(rtl)
                sources = [p for p in (ROOT/'rtl_output').glob('*.sv')
                           if p.name != 'ddr4_controller_top.sv'] + [top]
                bank_port = ".txn_bank('0)," if banks > 1 else ''
                bench = r'''
module tb;
    logic clk=0, rst_n=0, txn_valid=0, txn_is_write=1;
    always #5 clk=~clk;
    logic [3:0] txn_addr=0;
    logic [31:0] txn_wdata=0;
    logic txn_ready,cmd_ready,rsp_valid;
    logic [31:0] rsp_rdata;
    int sent=0,dispatched=0,completed=0,responses=0;
    bit seen[16];
    bit full_stall=0, simultaneous=0, full_reuse=0, consecutive=0, prev_accept=0;
    ddr4_controller_top dut(.clk(clk),.rst_n(rst_n),.txn_valid(txn_valid),
        .txn_is_write(txn_is_write),.txn_addr(txn_addr),.txn_wdata(txn_wdata),
        BANK_PORT .txn_ready(txn_ready),.cmd_ready(cmd_ready),
        .rsp_valid(rsp_valid),.rsp_rdata(rsp_rdata));
    always @(posedge clk) if(rst_n) begin
        if(txn_valid && txn_ready) begin
            sent++;
            if(prev_accept) consecutive=1;
        end
        prev_accept=txn_valid && txn_ready;
        if(txn_valid && !txn_ready) full_stall=1;
        if(dut.enqueue_fire && dut.dispatch_fire) begin
            simultaneous=1;
            if(dut.queue_full) full_reuse=1;
        end
        if(dut.dispatch_fire) begin
            int addr;
            addr=int'(dut.selected_addr);
            if(seen[addr]) $fatal(1,"Duplicate dispatch %0d",addr);
            seen[addr]=1;
            if(dut.selected_req.wdata !== 32'habc00000+32'(addr))
                $fatal(1,"Queued payload mismatch");
            dispatched++;
        end
        if(dut.completion_fire) completed++;
        if(rsp_valid) begin
            responses++;
            if(rsp_rdata !== 0) $fatal(1,"Read response data mismatch");
        end
        #1;
        if(int'(dut.queue_count) != sent-dispatched) $fatal(1,"Occupancy mismatch");
        if(dut.queue_full !== (dut.queue_count==4)) $fatal(1,"Full mismatch");
        if(dut.queue_empty !== (dut.queue_count==0)) $fatal(1,"Empty mismatch");
        if(dut.requests_accepted != sent || dut.requests_dispatched != dispatched ||
           dut.requests_completed != completed) $fatal(1,"Event count mismatch");
    end
    initial begin
        repeat(3) @(negedge clk);
        if(txn_ready) $fatal(1,"Ready during reset");
        rst_n=1;
        for(int n=0;n<16;n++) begin
            @(negedge clk);
            txn_valid=1; txn_addr=4'(n); txn_wdata=32'habc00000+32'(n);
            // Half writes and half reads; every address distinct, avoiding
            // dependence on Phase 4 same-address ordering policy.
            txn_is_write=(n<8);
            do @(posedge clk); while(!txn_ready);
        end
        @(negedge clk); txn_valid=0;
        wait(completed==16);
        repeat(3) @(negedge clk);
        if(sent!=16 || dispatched!=16 || responses!=8) $fatal(1,"Lost/duplicate requests");
        if(!full_stall || !simultaneous || !full_reuse || !consecutive) $fatal(1,"Missing handshake coverage");
        for(int n=0;n<8;n++)
            if(dut.bank_mem[0][n] !== 32'habc00000+32'(n)) $fatal(1,"Write lost");
        rst_n=0;
        repeat(2) @(negedge clk);
        if(dut.queue_count!=0 || dut.requests_accepted!=0) $fatal(1,"Reset failed");
        $display("PHASE2 PASS"); $finish;
    end
    initial begin #100000; $fatal(1,"Timeout"); end
endmodule
'''.replace('BANK_PORT', bank_port)
                simulate(self, directory, sources, bench)


if __name__ == '__main__':
    unittest.main()
