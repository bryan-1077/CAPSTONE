package dut_pkg;

    import uvm_pkg::*;
    `include "uvm_macros.svh"

    parameter int DEPTH = 4;
    parameter int REQUEST_WIDTH = 51;

    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    class dut_seq_item extends uvm_sequence_item;
        rand logic        deq_en;
        rand logic [1:0]  sel_idx;
        rand logic [50:0] enq_req;
        rand logic        enq_valid;
        logic             rst_pre;
        logic             deq_en_pre;
        logic [1:0]       sel_idx_pre;
        logic [50:0]      enq_req_pre;
        logic             enq_valid_pre;
        logic             enq_ready_pre;
        logic [203:0]     req_array_post;
        logic [3:0]       req_valid_post;

        `uvm_object_utils_begin(dut_seq_item)
            `uvm_field_int(deq_en, UVM_DEFAULT)
            `uvm_field_int(sel_idx, UVM_DEFAULT)
            `uvm_field_int(enq_req, UVM_DEFAULT)
            `uvm_field_int(enq_valid, UVM_DEFAULT)
        `uvm_object_utils_end

        function new(string name = "dut_seq_item");
            super.new(name);
        endfunction
    endclass

    class dut_sequencer extends uvm_sequencer#(dut_seq_item);
        `uvm_component_utils(dut_sequencer)
        virtual dut_if m_vif;

        function new(string name, uvm_component parent);
            super.new(name, parent);
        endfunction

        virtual function void build_phase(uvm_phase phase);
            super.build_phase(phase);
            if (!uvm_config_db#(virtual dut_if)::get(this, "", "vif", m_vif))
                `uvm_fatal("SEQ", "Could not get vif")
        endfunction
    endclass

    class dut_sequence extends uvm_sequence#(dut_seq_item);
        `uvm_object_utils(dut_sequence)
        `uvm_declare_p_sequencer(dut_sequencer)

        function new(string name = "dut_sequence");
            super.new(name);
        endfunction

        virtual task body();
            dut_seq_item req;

            repeat(3) begin
                @(p_sequencer.m_vif.driver_cb);
            end

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h1_001_01_1_00000001;
            req.enq_valid = 1;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h2_002_02_0_00000002;
            req.enq_valid = 1;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h0_003_03_1_00000003;
            req.enq_valid = 1;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h1_004_04_0_00000004;
            req.enq_valid = 1;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h2_005_05_1_00000005;
            req.enq_valid = 1;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 1;
            req.sel_idx = 1;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h3_006_06_0_00000006;
            req.enq_valid = 1;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 1;
            req.sel_idx = 2;
            req.enq_req = 51'h1_007_07_1_00000007;
            req.enq_valid = 1;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 1;
            req.sel_idx = 0;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 1;
            req.sel_idx = 2;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 1;
            req.sel_idx = 3;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 1;
            req.sel_idx = 1;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

            repeat(20) begin
                req = dut_seq_item::type_id::create("req");
                start_item(req);
                assert(req.randomize());
                finish_item(req);
            end

            req = dut_seq_item::type_id::create("req");
            start_item(req);
            req.deq_en = 0;
            req.sel_idx = 0;
            req.enq_req = 51'h0;
            req.enq_valid = 0;
            finish_item(req);

        endtask
    endclass

    class dut_driver extends uvm_driver#(dut_seq_item);
        `uvm_component_utils(dut_driver)
        virtual dut_if vif;

        function new(string name, uvm_component parent);
            super.new(name, parent);
        endfunction

        virtual function void build_phase(uvm_phase phase);
            super.build_phase(phase);
            if (!uvm_config_db#(virtual dut_if)::get(this, "", "vif", vif))
                `uvm_fatal("DRV", "Could not get vif")
        endfunction

        virtual task _agent2_generated_run_phase(uvm_phase phase);
            dut_seq_item req;
            vif.rst_n = 1'b1;
            vif.deq_en = 0;
            vif.sel_idx = 0;
            vif.enq_req = 0;
            vif.enq_valid = 0;
            forever begin
                seq_item_port.get_next_item(req);
                @(vif.driver_cb);
                vif.driver_cb.deq_en <= req.deq_en;
                vif.driver_cb.sel_idx <= req.sel_idx;
                vif.driver_cb.enq_req <= req.enq_req;
                vif.driver_cb.enq_valid <= req.enq_valid;
                @(vif.monitor_cb);
                seq_item_port.item_done();
            end
        endtask
// AGENT2_RUNTIME_BEGIN driver startup (owned by Agent 2)
    task run_phase(uvm_phase phase);
        vif.agent2_startup_reset();
        _agent2_generated_run_phase(phase);
    endtask
// AGENT2_RUNTIME_END driver startup
endclass

    class dut_monitor extends uvm_monitor;
        `uvm_component_utils(dut_monitor)
        virtual dut_if vif;
        uvm_analysis_port#(dut_seq_item) ap;

        function new(string name, uvm_component parent);
            super.new(name, parent);
        endfunction

        virtual function void build_phase(uvm_phase phase);
            super.build_phase(phase);
            if (!uvm_config_db#(virtual dut_if)::get(this, "", "vif", vif))
                `uvm_fatal("MON", "Could not get vif")
            ap = new("ap", this);
        endfunction

        virtual task run_phase(uvm_phase phase);
            dut_seq_item tr;
            forever begin
                @(vif.monitor_cb);
                tr = dut_seq_item::type_id::create("tr");
                tr.rst_pre = vif.monitor_cb.rst_pre;
                tr.deq_en_pre = vif.monitor_cb.deq_en_pre;
                tr.sel_idx_pre = vif.monitor_cb.sel_idx_pre;
                tr.enq_req_pre = vif.monitor_cb.enq_req_pre;
                tr.enq_valid_pre = vif.monitor_cb.enq_valid_pre;
                tr.enq_ready_pre = vif.monitor_cb.enq_ready_pre;
                tr.req_array_post = vif.monitor_cb.req_array_post;
                tr.req_valid_post = vif.monitor_cb.req_valid_post;
                ap.write(tr);
            end
        endtask
    endclass

    class dut_agent extends uvm_agent;
        `uvm_component_utils(dut_agent)
        dut_driver drv;
        dut_monitor mon;
        dut_sequencer sqr;
        uvm_analysis_port#(dut_seq_item) ap;

        function new(string name, uvm_component parent);
            super.new(name, parent);
        endfunction

        virtual function void build_phase(uvm_phase phase);
            super.build_phase(phase);
            drv = dut_driver::type_id::create("drv", this);
            mon = dut_monitor::type_id::create("mon", this);
            sqr = dut_sequencer::type_id::create("sqr", this);
        endfunction

        virtual function void connect_phase(uvm_phase phase);
            super.connect_phase(phase);
            drv.seq_item_port.connect(sqr.seq_item_export);
            ap = mon.ap;
        endfunction
    endclass

    class dut_scoreboard extends uvm_scoreboard;
        `uvm_component_utils(dut_scoreboard)
        uvm_analysis_imp#(dut_seq_item, dut_scoreboard) sb_imp;

        int pass_cnt;
        int fail_cnt;

        request_t ref_data[DEPTH];
        logic [DEPTH-1:0] ref_valid;

        int req_checks[string];
        int req_failures[string];

        string first_evidence[string];
        string first_failure_evidence[string];

        function new(string name, uvm_component parent);
            super.new(name, parent);
        endfunction

        virtual function void build_phase(uvm_phase phase);
            string req_id;
            super.build_phase(phase);
            sb_imp = new("sb_imp", this);
            pass_cnt = 0;
            fail_cnt = 0;
            for (int i = 0; i < DEPTH; i++) begin
                ref_data[i] = '0;
            end
            ref_valid = '0;
            req_id = "REQ-002"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-003"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-004"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-005"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-006"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-007"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-008"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-009"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-010"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-011"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-012"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-013"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-014"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-015"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-016"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-017"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-021"; req_checks[req_id] = 0; req_failures[req_id] = 0;
            req_id = "REQ-022"; req_checks[req_id] = 0; req_failures[req_id] = 0;
        endfunction

        function void _agent2_generated_check_requirement(string id, logic condition, string scenario, string expected_str, string observed_str);
            string evidence;
            req_checks[id]++;
            evidence = $sformatf("%s|%0t|%s|%s", scenario, $time, expected_str, observed_str);
            if (!first_evidence.exists(id)) begin
                first_evidence[id] = evidence;
            end
            if (condition !== 1'b1) begin
                req_failures[id]++;
                if (!first_failure_evidence.exists(id)) begin
                    first_failure_evidence[id] = evidence;
                end
                fail_cnt++;
                `uvm_error("SB", $sformatf("%s FAIL at %0t: %s | Expected: %s | Observed: %s",
                    id, $time, scenario, expected_str, observed_str))
            end else begin
                pass_cnt++;
            end
        endfunction
// AGENT2_EVIDENCE_BEGIN canonical comparison records (owned by Agent 2)
bit _agent2_evidence_seen[string];
bit _agent2_failure_seen[string];
int _agent2_req_checks[string];
int _agent2_req_failures[string];
function void _agent2_report_requirements();
    string id;
    string status;
    foreach (_agent2_req_checks[id]) begin
        status = (_agent2_req_failures[id] > 0) ? "FAIL" : "PASS";
        `uvm_info("AGENT2_REPORT", $sformatf("AGENT2_REQ_COUNTS|%s|%0d|%0d",
            id, _agent2_req_checks[id], _agent2_req_failures[id]), UVM_NONE)
        `uvm_info("AGENT2_REPORT", $sformatf("AGENT2_REQ|%s|%s|%0d comparisons, %0d failures",
            id, status, _agent2_req_checks[id], _agent2_req_failures[id]), UVM_NONE)
    end
endfunction
function automatic string _agent2_evidence_field(string value);
    for (int i = 0; i < value.len(); i++) begin
        if (value.getc(i) == 8'd124 || value.getc(i) == 8'd10 || value.getc(i) == 8'd13)
            value.putc(i, 8'd32);
    end
    return value;
endfunction
function void check_requirement(string id, logic condition, string scenario, string expected_str, string observed_str);
    if (!_agent2_req_checks.exists(id)) begin
        _agent2_req_checks[id] = 0;
        _agent2_req_failures[id] = 0;
    end
    _agent2_req_checks[id]++;
    if (condition !== 1'b1) _agent2_req_failures[id]++;
    if (!_agent2_evidence_seen.exists(id) ||
        (condition !== 1'b1 && !_agent2_failure_seen.exists(id))) begin
        `uvm_info("AGENT2_EVIDENCE", $sformatf(
            "AGENT2_REQ_EVIDENCE|%s|%s|time=%0t|expected=%s|observed=%s",
            id, _agent2_evidence_field(scenario), $time,
            _agent2_evidence_field(expected_str), _agent2_evidence_field(observed_str)), UVM_NONE)
        _agent2_evidence_seen[id] = 1'b1;
        if (condition !== 1'b1) _agent2_failure_seen[id] = 1'b1;
    end
    _agent2_generated_check_requirement(id, condition, scenario, expected_str, observed_str);
endfunction
// AGENT2_EVIDENCE_END canonical comparison records


        virtual function void write(dut_seq_item tr);
            request_t enq_req_struct;
            logic [DEPTH-1:0] exp_valid_pre;
            logic exp_enq_ready_pre;
            logic [DEPTH-1:0] free_slots;
            int first_free_idx;
            logic has_free_slot;
            int insert_idx;
            logic reuse_slot;
            logic [DEPTH-1:0] exp_valid_post;
            request_t exp_data_post[DEPTH];
            logic [DEPTH-1:0] write_mask;
            logic [DEPTH-1:0] remove_mask;
            logic agent2_state_matches;
            string scenario;
            string expected_str;
            string observed_str;
            string exp_valid_pre_str;
            string exp_valid_post_str;
            string obs_valid_post_str;
            request_t obs_payload;

            enq_req_struct.bank = tr.enq_req_pre[50:49];
            enq_req_struct.row = tr.enq_req_pre[48:39];
            enq_req_struct.col = tr.enq_req_pre[38:33];
            enq_req_struct.is_write = tr.enq_req_pre[32];
            enq_req_struct.wdata = tr.enq_req_pre[31:0];

            if (tr.rst_pre === 1'b0) begin
                for (int i = 0; i < DEPTH; i++) begin
                    ref_data[i] = '0;
                end
                ref_valid = '0;
                return;
            end

            exp_valid_pre = ref_valid;

            for (int i = 0; i < DEPTH; i++) begin
                free_slots[i] = ~exp_valid_pre[i];
            end

            first_free_idx = 0;
            has_free_slot = 1'b0;
            for (int j = 0; j < DEPTH; j++) begin
                if (free_slots[j] && !has_free_slot) begin
                    first_free_idx = j;
                    has_free_slot = 1'b1;
                end
            end

            if (has_free_slot) begin
                insert_idx = first_free_idx;
            end else begin
                insert_idx = int'(tr.sel_idx_pre);
            end

            reuse_slot = (tr.deq_en_pre === 1'b1) && (tr.enq_valid_pre === 1'b1) && (insert_idx == int'(tr.sel_idx_pre));

            exp_enq_ready_pre = has_free_slot || ((~(|free_slots)) && (tr.deq_en_pre === 1'b1));

            scenario = $sformatf("enq_v=%0b deq=%0b idx=%0d reuse=%0b pre_valid=%04b",
                tr.enq_valid_pre, tr.deq_en_pre, tr.sel_idx_pre, reuse_slot, exp_valid_pre);
            expected_str = $sformatf("enq_ready_pre=%0b", exp_enq_ready_pre);
            observed_str = $sformatf("enq_ready_pre=%0b", tr.enq_ready_pre);
            check_requirement("REQ-003", (tr.enq_ready_pre === exp_enq_ready_pre), scenario, expected_str, observed_str);
            check_requirement("REQ-012", (tr.enq_ready_pre === exp_enq_ready_pre), scenario, expected_str, observed_str);

            write_mask = '0;
            remove_mask = '0;
            exp_valid_post = exp_valid_pre;
            for (int i = 0; i < DEPTH; i++) begin
                exp_data_post[i] = ref_data[i];
            end

            if ((tr.deq_en_pre === 1'b1) && !reuse_slot) begin
                remove_mask[tr.sel_idx_pre] = 1'b1;
                exp_valid_post[tr.sel_idx_pre] = 1'b0;
            end

            if ((tr.enq_valid_pre === 1'b1) && (tr.enq_ready_pre === 1'b1)) begin
                write_mask[insert_idx] = 1'b1;
                exp_data_post[insert_idx] = enq_req_struct;
                exp_valid_post[insert_idx] = 1'b1;
            end

            agent2_state_matches = (tr.req_valid_post === exp_valid_post);
            for (int k = 0; k < DEPTH; k++) begin
                if (exp_valid_post[k]) begin
                    obs_payload = request_t'(tr.req_array_post[k*REQUEST_WIDTH +: REQUEST_WIDTH]);
                    agent2_state_matches &= (obs_payload === exp_data_post[k]);
                end
            end

            exp_valid_post_str = $sformatf("%04b", exp_valid_post);
            obs_valid_post_str = $sformatf("%04b", tr.req_valid_post);
            scenario = $sformatf("enq_v=%0b deq=%0b idx=%0d reuse=%0b", tr.enq_valid_pre, tr.deq_en_pre, tr.sel_idx_pre, reuse_slot);
            expected_str = $sformatf("valid=%s", exp_valid_post_str);
            observed_str = $sformatf("valid=%s", obs_valid_post_str);
            check_requirement("REQ-002", agent2_state_matches, scenario, expected_str, observed_str);
            check_requirement("REQ-011", agent2_state_matches, scenario, expected_str, observed_str);
            check_requirement("REQ-016", agent2_state_matches, scenario, expected_str, observed_str);
            check_requirement("REQ-017", agent2_state_matches, scenario, expected_str, observed_str);

            if ((tr.enq_valid_pre === 1'b1) && (tr.enq_ready_pre === 1'b1)) begin
                if (has_free_slot) begin
                    scenario = $sformatf("enqueue to first_free=%0d", first_free_idx);
                    expected_str = $sformatf("insert_idx=%0d", first_free_idx);
                    observed_str = $sformatf("insert_idx=%0d valid[%0d]=%0b", insert_idx, first_free_idx, tr.req_valid_post[first_free_idx]);
                    check_requirement("REQ-004", (tr.req_valid_post[first_free_idx] === 1'b1), scenario, expected_str, observed_str);
                    check_requirement("REQ-013", (tr.req_valid_post[first_free_idx] === 1'b1), scenario, expected_str, observed_str);
                end else if (tr.deq_en_pre === 1'b1) begin
                    obs_payload = request_t'(tr.req_array_post[tr.sel_idx_pre*REQUEST_WIDTH +: REQUEST_WIDTH]);
                    scenario = $sformatf("full enqueue+deq to sel_idx=%0d", tr.sel_idx_pre);
                    expected_str = $sformatf("valid[%0d]=1 data=%013h", tr.sel_idx_pre, enq_req_struct);
                    observed_str = $sformatf("valid[%0d]=%0b data=%013h", tr.sel_idx_pre, tr.req_valid_post[tr.sel_idx_pre], obs_payload);
                    check_requirement("REQ-004", (tr.req_valid_post[tr.sel_idx_pre] === 1'b1) && (obs_payload === enq_req_struct), scenario, expected_str, observed_str);
                    check_requirement("REQ-013", (tr.req_valid_post[tr.sel_idx_pre] === 1'b1) && (obs_payload === enq_req_struct), scenario, expected_str, observed_str);
                end
            end

            if (reuse_slot) begin
                obs_payload = request_t'(tr.req_array_post[insert_idx*REQUEST_WIDTH +: REQUEST_WIDTH]);
                scenario = $sformatf("same-index enq+deq idx=%0d", insert_idx);
                expected_str = $sformatf("valid[%0d]=1 data=%013h", insert_idx, enq_req_struct);
                observed_str = $sformatf("valid[%0d]=%0b data=%013h", insert_idx, tr.req_valid_post[insert_idx], obs_payload);
                check_requirement("REQ-005", (tr.req_valid_post[insert_idx] === 1'b1) && (obs_payload === enq_req_struct), scenario, expected_str, observed_str);
                check_requirement("REQ-006", (tr.req_valid_post[insert_idx] === 1'b1) && (obs_payload === enq_req_struct), scenario, expected_str, observed_str);
                check_requirement("REQ-007", (tr.req_valid_post[insert_idx] === 1'b1), scenario, expected_str, observed_str);
                check_requirement("REQ-008", (tr.req_valid_post[insert_idx] === 1'b1), scenario, expected_str, observed_str);
                check_requirement("REQ-014", (tr.req_valid_post[insert_idx] === 1'b1) && (obs_payload === enq_req_struct), scenario, expected_str, observed_str);
                check_requirement("REQ-015", (tr.req_valid_post[insert_idx] === 1'b1), scenario, expected_str, observed_str);
            end

            for (int m = 0; m < DEPTH; m++) begin
                if (exp_valid_pre[m] && !write_mask[m] && !remove_mask[m]) begin
                    obs_payload = request_t'(tr.req_array_post[m*REQUEST_WIDTH +: REQUEST_WIDTH]);
                    scenario = $sformatf("persist slot=%0d enq_v=%0b deq=%0b", m, tr.enq_valid_pre, tr.deq_en_pre);
                    expected_str = $sformatf("valid[%0d]=1 data=%013h", m, ref_data[m]);
                    observed_str = $sformatf("valid[%0d]=%0b data=%013h", m, tr.req_valid_post[m], obs_payload);
                    check_requirement("REQ-009", (tr.req_valid_post[m] === 1'b1) && (obs_payload === ref_data[m]), scenario, expected_str, observed_str);
                    check_requirement("REQ-010", (tr.req_valid_post[m] === 1'b1) && (obs_payload === ref_data[m]), scenario, expected_str, observed_str);
                    check_requirement("REQ-021", (tr.req_valid_post[m] === 1'b1) && (obs_payload === ref_data[m]), scenario, expected_str, observed_str);
                    check_requirement("REQ-022", (tr.req_valid_post[m] === 1'b1) && (obs_payload === ref_data[m]), scenario, expected_str, observed_str);
                end
            end

            ref_valid = exp_valid_post;
            for (int n = 0; n < DEPTH; n++) begin
                ref_data[n] = exp_data_post[n];
            end
        endfunction

        virtual function void _agent2_generated_report_phase(uvm_phase phase);
            string status;
            string evidence;
            string req_id;
            string req_ids[$];
            super.report_phase(phase);

            `uvm_info("SB", "=== SCOREBOARD REPORT ===", UVM_NONE)
            `uvm_info("SB", $sformatf("PASS=%0d FAIL=%0d", pass_cnt, fail_cnt), UVM_NONE)

            req_ids.push_back("REQ-001");
            req_ids.push_back("REQ-002");
            req_ids.push_back("REQ-003");
            req_ids.push_back("REQ-004");
            req_ids.push_back("REQ-005");
            req_ids.push_back("REQ-006");
            req_ids.push_back("REQ-007");
            req_ids.push_back("REQ-008");
            req_ids.push_back("REQ-009");
            req_ids.push_back("REQ-010");
            req_ids.push_back("REQ-011");
            req_ids.push_back("REQ-012");
            req_ids.push_back("REQ-013");
            req_ids.push_back("REQ-014");
            req_ids.push_back("REQ-015");
            req_ids.push_back("REQ-016");
            req_ids.push_back("REQ-017");
            req_ids.push_back("REQ-018");
            req_ids.push_back("REQ-019");
            req_ids.push_back("REQ-020");
            req_ids.push_back("REQ-021");
            req_ids.push_back("REQ-022");
            req_ids.push_back("REQ-023");
            req_ids.push_back("REQ-024");
            req_ids.push_back("REQ-025");
            req_ids.push_back("REQ-026");
            req_ids.push_back("REQ-027");
            req_ids.push_back("REQ-028");

            foreach(req_ids[i]) begin
                req_id = req_ids[i];
                if (req_checks.exists(req_id)) begin
                    if (req_failures[req_id] > 0) begin
                        status = "FAIL";
                        evidence = $sformatf("Failed %0d/%0d checks", req_failures[req_id], req_checks[req_id]);
                    end else if (req_checks[req_id] > 0) begin
                        status = "PASS";
                        evidence = $sformatf("Passed %0d checks", req_checks[req_id]);
                    end else begin
                        status = "NOT_TESTED";
                        evidence = "No checks executed";
                    end
                end else begin
                    status = "NOT_TESTED";
                    if (req_id == "REQ-001") evidence = "Static: request_t has no valid field (source review)";
                    else if (req_id == "REQ-018") evidence = "Static: DEPTH parameter default (source review)";
                    else if (req_id == "REQ-019") evidence = "Static: request_t typedef usage (source review)";
                    else if (req_id == "REQ-020") evidence = "Static: no valid field in typedef (source review)";
                    else if (req_id == "REQ-023") evidence = "Static: sequential ordering (source review)";
                    else if (req_id == "REQ-024") evidence = "Static: reuse_slot signal (source review)";
                    else if (req_id == "REQ-025") evidence = "Static: depth_default value (source review)";
                    else if (req_id == "REQ-026") evidence = "Static: typedef name (source review)";
                    else if (req_id == "REQ-027") evidence = "Static: packed structure (source review)";
                    else if (req_id == "REQ-028") evidence = "Static: field widths (source review)";
                    else evidence = "Not applicable to simulation";
                end
                `uvm_info("SB", $sformatf("AGENT2_REQ|%s|%s|%s", req_id, status, evidence), UVM_NONE)
            end

            foreach(req_ids[i]) begin
                req_id = req_ids[i];
                if (req_checks.exists(req_id)) begin
                    `uvm_info("SB", $sformatf("AGENT2_REQ_COUNTS|%s|%0d|%0d", req_id, req_checks[req_id], req_failures[req_id]), UVM_NONE)
                end else begin
                    `uvm_info("SB", $sformatf("AGENT2_REQ_COUNTS|%s|0|0", req_id), UVM_NONE)
                end
            end
        endfunction
// AGENT2_REPORT_BEGIN per-requirement counts from executed comparisons
function void report_phase(uvm_phase phase);
    _agent2_generated_report_phase(phase);
    _agent2_report_requirements();
endfunction
// AGENT2_REPORT_END

    endclass

    class dut_env extends uvm_env;
        `uvm_component_utils(dut_env)
        dut_agent agt;
        dut_scoreboard sb;

        function new(string name, uvm_component parent);
            super.new(name, parent);
        endfunction

        virtual function void build_phase(uvm_phase phase);
            super.build_phase(phase);
            agt = dut_agent::type_id::create("agt", this);
            sb = dut_scoreboard::type_id::create("sb", this);
        endfunction

        virtual function void connect_phase(uvm_phase phase);
            super.connect_phase(phase);
            agt.ap.connect(sb.sb_imp);
        endfunction
    endclass

    class dut_test extends uvm_test;
        `uvm_component_utils(dut_test)
        dut_env env;
        dut_sequence seq;

        function new(string name, uvm_component parent);
            super.new(name, parent);
        endfunction

        virtual function void build_phase(uvm_phase phase);
            super.build_phase(phase);
            env = dut_env::type_id::create("env", this);
            seq = dut_sequence::type_id::create("seq");
        endfunction

        virtual task run_phase(uvm_phase phase);
            phase.raise_objection(this);
            seq.start(env.agt.sqr);
            #100;
            phase.drop_objection(this);
        endtask
    endclass

endpackage
