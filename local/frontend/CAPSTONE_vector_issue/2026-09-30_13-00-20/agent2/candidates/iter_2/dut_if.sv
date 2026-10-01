interface dut_if(input logic clk);
    logic        rst_n;
    logic        deq_en;
    logic [1:0]  sel_idx;
    logic [50:0] enq_req;
    logic        enq_valid;
    logic        enq_ready;
    logic [203:0] req_array;
    logic [3:0]  req_valid;

    clocking monitor_cb @(posedge clk);
        input #1step rst_pre = rst_n;
        input #1step deq_en_pre = deq_en;
        input #1step sel_idx_pre = sel_idx;
        input #1step enq_req_pre = enq_req;
        input #1step enq_valid_pre = enq_valid;
        input #1step enq_ready_pre = enq_ready;
        input #0 req_array_post = req_array;
        input #0 req_valid_post = req_valid;
    endclocking

    clocking driver_cb @(negedge clk);
        output deq_en;
        output sel_idx;
        output enq_req;
        output enq_valid;
    endclocking

// AGENT2_RUNTIME_BEGIN startup reset (owned by Agent 2)
    clocking agent2_reset_cb @(posedge clk);
        input #1step sampled_reset = rst_n;
    endclocking
    task automatic agent2_startup_reset();
        int unsigned sampled_edges;
        sampled_edges = 0;
        
        rst_n = 1'b0;
        @(negedge clk);
        repeat (2) begin
            @(agent2_reset_cb);
            if (agent2_reset_cb.sampled_reset !== 1'b0)
                uvm_pkg::uvm_report_fatal("AGENT2_TB_RESET",
                    "Startup reset was not active at the DUT consuming edge");
            sampled_edges++;
        end
        @(negedge clk);
        rst_n = 1'b1;
        uvm_pkg::uvm_report_info("AGENT2_TB_RESET",
            $sformatf("AGENT2_RESET|rst_n|READY|%0d|time=%0t", sampled_edges, $time),
            uvm_pkg::UVM_NONE);
    endtask
// AGENT2_RUNTIME_END startup reset
endinterface
