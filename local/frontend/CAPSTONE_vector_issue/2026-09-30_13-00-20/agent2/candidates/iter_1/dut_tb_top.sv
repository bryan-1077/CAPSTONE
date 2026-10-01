module dut_tb_top;
    import uvm_pkg::*;
    import dut_pkg::*;

    logic clk;

    dut_if vif(clk);

    ddr4_request_queue #(.DEPTH(4)) dut (
        .clk(clk),
        .rst_n(vif.rst_n),
        .deq_en(vif.deq_en),
        .sel_idx(vif.sel_idx),
        .enq_req(vif.enq_req),
        .enq_valid(vif.enq_valid),
        .enq_ready(vif.enq_ready),
        .req_array(vif.req_array),
        .req_valid(vif.req_valid)
    );

    initial begin
        clk = 0;
        forever #5 clk = ~clk;
    end

    initial begin
        uvm_config_db#(virtual dut_if)::set(null, "*", "vif", vif);
        run_test("dut_test");
    end

endmodule
