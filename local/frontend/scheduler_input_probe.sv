`timescale 1ns/1ps
// Compare the original checker input packing with directly packed stimulus.
module scheduler_input_probe;
    logic clk = 0;
    always #5 clk = ~clk;
    logic rst_n = 0;
    logic ref_req = 0, ready = 0;
    logic [3:0] bank_active = 0;
    typedef struct packed {
        logic [1:0] bank;
        logic [9:0] row;
        logic [5:0] col;
        logic is_write;
        logic [31:0] wdata;
    } request_t;
    request_t requests [4];
    logic valid [4];
    logic [9:0] rows [4];
    wire [203:0] array_requests;
    wire [3:0] array_valid;
    wire [39:0] array_rows;
    logic [203:0] direct_requests = 0;
    logic [3:0] direct_valid = 0;
    logic [39:0] direct_rows = 0;
    wire array_issue_ref, array_issue_txn, array_issue_valid;
    wire direct_issue_ref, direct_issue_txn, direct_issue_valid;
    wire [1:0] array_idx, direct_idx;
    int errors = 0;

    assign array_requests[50:0] = requests[0];
    assign array_requests[101:51] = requests[1];
    assign array_requests[152:102] = requests[2];
    assign array_requests[203:153] = requests[3];
    assign array_valid = {valid[3], valid[2], valid[1], valid[0]};
    assign array_rows[9:0] = rows[0];
    assign array_rows[19:10] = rows[1];
    assign array_rows[29:20] = rows[2];
    assign array_rows[39:30] = rows[3];

    ddr4_scheduler_scheduler array_dut (
        .clk(clk), .rst_n(rst_n), .ref_req(ref_req), .cmd_ready(ready), .timing_ok(1'b1),
        .bank_active(bank_active), .bank_open_row(array_rows),
        .req_array(array_requests), .req_valid(array_valid), .sel_idx(array_idx),
        .issue_ref(array_issue_ref), .issue_txn(array_issue_txn), .issue_valid(array_issue_valid)
    );
    ddr4_scheduler_scheduler direct_dut (
        .clk(clk), .rst_n(rst_n), .ref_req(ref_req), .cmd_ready(ready), .timing_ok(1'b1),
        .bank_active(bank_active), .bank_open_row(direct_rows),
        .req_array(direct_requests), .req_valid(direct_valid), .sel_idx(direct_idx),
        .issue_ref(direct_issue_ref), .issue_txn(direct_issue_txn), .issue_valid(direct_issue_valid)
    );

    task automatic check_case(input string name, input logic refresh, input logic [3:0] validity,
                              input logic cmd_ready, input logic [3:0] active,
                              input logic expect_ref, input logic expect_txn, input logic [1:0] expect_idx);
        @(negedge clk);
        ref_req = refresh;
        ready = cmd_ready;
        bank_active = active;
        valid[0] = validity[0]; valid[1] = validity[1];
        valid[2] = validity[2]; valid[3] = validity[3];
        requests[0] = '0; requests[1] = '0; requests[2] = '0; requests[3] = '0;
        requests[0].bank = 0; requests[0].row = 3;
        requests[1].bank = 1; requests[1].row = 9;
        rows[0] = 0; rows[1] = 9; rows[2] = 0; rows[3] = 0;
        direct_valid = validity;
        direct_requests = {requests[3], requests[2], requests[1], requests[0]};
        direct_rows = {rows[3], rows[2], rows[1], rows[0]};
        @(posedge clk); #1;
        $display("[PROBE] %s time=%0t source_valid=%b array_port=%b direct_port=%b inputs_equal=%b array_out(ref,txn,valid,idx)=%b,%b,%b,%0d direct_out=%b,%b,%b,%0d",
                 name, $time, {valid[3],valid[2],valid[1],valid[0]}, array_valid, direct_valid,
                 (array_requests === direct_requests) && (array_rows === direct_rows) && (array_valid === direct_valid),
                 array_issue_ref, array_issue_txn, array_issue_valid, array_idx,
                 direct_issue_ref, direct_issue_txn, direct_issue_valid, direct_idx);
        if (array_valid !== direct_valid || array_requests !== direct_requests || array_rows !== direct_rows) begin
            $display("[ERROR] %s: unpacked-array stimulus did not propagate to packed ports", name);
            errors++;
        end
        if ({array_issue_ref,array_issue_txn,array_idx} !== {expect_ref,expect_txn,expect_idx}) begin
            $display("[ERROR] %s: array-driven scheduler outputs differ from expectation", name);
            errors++;
        end
        if ({direct_issue_ref,direct_issue_txn,direct_idx} !== {expect_ref,expect_txn,expect_idx}) begin
            $display("[ERROR] %s: directly packed scheduler outputs differ from expectation", name);
            errors++;
        end
    endtask

    initial begin
        valid[0]=0; valid[1]=0; valid[2]=0; valid[3]=0;
        requests[0]='0; requests[1]='0; requests[2]='0; requests[3]='0;
        rows[0]=0; rows[1]=0; rows[2]=0; rows[3]=0;
        repeat (4) @(posedge clk);
        @(negedge clk); rst_n=1;
        check_case("idle", 0, 0, 1, 0, 0, 0, 0);
        check_case("refresh", 1, 0, 1, 0, 1, 0, 0);
        check_case("transaction", 0, 1, 1, 0, 0, 1, 0);
        check_case("blocked contention", 1, 1, 0, 0, 0, 0, 0);
        check_case("refresh priority", 1, 1, 1, 0, 1, 0, 0);
        check_case("transaction again", 0, 1, 1, 0, 0, 1, 0);
        check_case("idle before row hit", 0, 0, 1, 0, 0, 0, 0);
        check_case("row-hit request 1", 0, 3, 1, 2, 0, 1, 1);
        if (errors) $fatal(1, "PROBE FAIL: %0d errors", errors);
        $display("PROBE PASS");
        $finish;
    end
endmodule
