module ddr4_tFAW_tFAW_tracker (
    input logic clk,
    input logic rst_n,
    input logic act_valid,
    output logic [2:0] act_count,
    output logic tFAW_ok
);

    logic [7:0] cycle_counter;
    logic [7:0] act_timestamps[3:0];
    
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            cycle_counter <= 8'b0;
            act_timestamps[0] <= 8'b0;
            act_timestamps[1] <= 8'b0;
            act_timestamps[2] <= 8'b0;
            act_timestamps[3] <= 8'b0;
            act_count <= 3'b0;
        end else begin
            // Increment and wrap cycle_counter
            cycle_counter <= cycle_counter + 1;
            
            if (act_valid) begin
                // Shift timestamps
                act_timestamps[3] <= act_timestamps[2];
                act_timestamps[2] <= act_timestamps[1];
                act_timestamps[1] <= act_timestamps[0];
                act_timestamps[0] <= cycle_counter;
                
                // Increment act_count if less than 4
                if (act_count < 3'b100) begin
                    act_count <= act_count + 1;
                end
            end
        end
    end

    always_comb begin
        if (act_count < 3'b100) begin
            tFAW_ok = 1'b1;
        end else begin
            tFAW_ok = (cycle_counter - act_timestamps[3]) >= 8'd30;
        end
    end

endmodule