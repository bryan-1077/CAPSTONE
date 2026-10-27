"""Isolated four-bank timing permission tests with explicit bank-state stubs.

Checks component input/output semantics, not reachability of every stimulus from
a complete controller. Histories come only from supplied ACT commit events.
"""
from collections import Counter
import random
import re

from simulation.controller import CheckFailure

PROFILE = 'ddr4-timing-guard-v1'
CHECKS = ('reset_and_invalid','tRCD_per_bank','tRAS_per_bank','tRRD_global','tFAW_window',
          'probes_no_history','reset_history','long_idle','seeded_history')
PORTS = {'candidate_command':('input',45),'candidate_valid':('input',1),
         'commit_command':('input',45),'commit_valid':('input',1),
         'open_rows':('input',16),'open_valid':('input',4),'candidate_ok':('output',1)}


def profile_check(h):
    if h['phase'] != 'rtl':raise ValueError('Timing guard requires an rtl-phase handoff')
    modules=[m for m in h['design']['modules'] if 'timing' in m.get('roles',[])]
    if len(modules)!=1:raise ValueError('Timing guard requires exactly one timing-role module')
    module=modules[0]
    if {p['name']:(p['direction'],p['width']) for p in module['ports']}!=PORTS:
        raise ValueError('Timing module interface is outside the isolated guard profile')
    if module['clock'].get('name')!='clk' or module['reset']!={'name':'rst_n','active_low':True,'synchronous':True}:
        raise ValueError('Timing guard requires clk and synchronous active-low rst_n')
    shared=h['shared'];p=shared['parameters'];types=shared['types']
    if any(p.get(k)!=v for k,v in {'BANK_COUNT':4,'ROW_WIDTH':4,'DATA_WIDTH':32,'TFAW_ACT_LIMIT':4}.items()):
        raise ValueError('Timing guard requires four 4-bit-row banks, 32-bit data and four-ACT limit')
    fields=[(f['name'],f['width'],f['lsb']) for f in types['command']['fields']]
    if fields!=[('opcode',3,42),('bank',2,40),('row',4,36),('column',4,32),('wdata',32,0)]:
        raise ValueError('Unsupported command layout')
    if types['opcode']['encodings']!={'NOP':0,'ACT':1,'PRE':2,'READ':3,'WRITE':4}:
        raise ValueError('Unsupported command vocabulary')
    rows,valid=types['bank_rows'],types['bank_valid']
    for typ,width in ((rows,4),(valid,1)):
        if any(typ.get(k)!=v for k,v in {'kind':'array','count':4,'element_width':width,
                                        'packed_order':'ascending_index_from_lsb'}.items()):
            raise ValueError('Unsupported bank-state stub layout')
    timing=shared['timing_profile']
    if timing['unit']!='cycles' or set(timing['values'])!={'tRCD','tRAS','tRRD','tFAW'}:
        raise ValueError('Timing guard supports exactly tRCD/tRAS/tRRD/tFAW cycle minima')
    t=timing['values']
    if any(type(v) is not int or not 2<=v<=100000 for v in t.values()) or t['tFAW']<=3*t['tRRD']+1:
        raise ValueError('Timing values do not support the four legally spaced ACT window probes')
    return module


def command(opcode,bank=0,row=0,column=0,data=0):
    return (opcode<<42)|(bank<<40)|(row<<36)|(column<<32)|data


def vector(*, reset=False, amount=1, candidate=0, valid=0, commit=0, committed=0, banks=0, rows=0):
    packed=candidate|(commit<<45)|(valid<<90)|(committed<<91)
    return (0 if reset else 1,amount,banks,rows,packed)


def scenarios(shared,seed):
    t=shared['timing_profile']['values']
    reset=[vector(reset=True,amount=2,candidate=command(1),valid=1,commit=command(1),committed=1)]
    cases={name:[] for name in CHECKS}
    cases['reset_and_invalid']=reset+[vector(candidate=command(1),valid=1),
        vector(candidate=command(1)),vector(candidate=command(0),valid=1),vector(candidate=command(7),valid=1)]
    for name,timing,ops in (('tRCD_per_bank','tRCD',(3,4)),('tRAS_per_bank','tRAS',(2,))):
        for op in ops:
            for age in sorted({1,t[timing]-1,t[timing],t[timing]+1}):
                case=cases[name]
                case+=reset+[vector(commit=command(1,0,3),committed=1)]
                if age>1:case.append(vector(amount=age-1))
                case.append(vector(candidate=command(op,0,3),valid=1,banks=1,rows=3))
                # An open other bank with no ACT history gets no synthetic delay.
                case.append(vector(candidate=command(op,1,4),valid=1,banks=3,rows=0x43))
    for bank in (1,2,3):
        for age in sorted({1,t['tRRD']-1,t['tRRD'],t['tRRD']+1}):
            cases['tRRD_global']+=reset+[vector(commit=command(1,0,3),committed=1)]
            if age>1:cases['tRRD_global'].append(vector(amount=age-1))
            cases['tRRD_global'].append(vector(candidate=command(1,bank,4),valid=1))
    for age in (t['tFAW']-1,t['tFAW'],t['tFAW']+1):
        case=cases['tFAW_window'];case+=reset+[vector(commit=command(1,0,3),committed=1)]
        for bank in (1,2,3):
            if t['tRRD']>1:case.append(vector(amount=t['tRRD']-1))
            case.append(vector(commit=command(1,bank,3),committed=1))
        idle=age-3*t['tRRD']-1
        if idle:case.append(vector(amount=idle))
        # Stub closed-bank input intentionally isolates tFAW from controller row lifecycle.
        case.append(vector(candidate=command(1),valid=1,commit=command(1),committed=int(age==t['tFAW'])))
        case.append(vector(candidate=command(1,1),valid=1))
    cases['probes_no_history']=reset+[vector(candidate=command(1),valid=1,amount=12),
        vector(commit=command(1)),vector(candidate=command(1),valid=1)]
    for op in (0,2,3,4,7):
        cases['probes_no_history'] += [vector(commit=command(op),committed=1),vector(candidate=command(1),valid=1)]
    cases['reset_history']=reset+[vector(commit=command(1),committed=1),
        vector(candidate=command(1,1),valid=1)]+reset+[vector(candidate=command(1,1),valid=1),
        vector(candidate=command(3,0,3),valid=1,banks=1,rows=3)]
    cases['long_idle']=reset+[vector(commit=command(1,0,3),committed=1),
        vector(amount=max(1024,8*max(t.values()))),vector(candidate=command(2,0),valid=1,banks=1,rows=3),
        vector(candidate=command(3,0,3),valid=1,banks=1,rows=3),vector(candidate=command(1,1),valid=1)]
    rng=random.Random(seed)
    cases['seeded_history']=list(reset)
    for c in range(512):
        cases['seeded_history'].append(vector(candidate=command(rng.randrange(8),rng.randrange(4),rng.randrange(16)),
            valid=rng.randrange(2),commit=command(1,(c//(2*t['tRRD']))%4,3),
            committed=int(c%(2*t['tRRD'])==0),banks=rng.randrange(16),rows=rng.getrandbits(16)))
    return cases


def testbench(h):
    module=profile_check(h)
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_$]*',module['module_name']):
        raise ValueError('Invalid module identifier')
    return f'''`timescale 1ns/1ps
module capstone_sim_tb;
logic clk, rst_n, candidate_valid, commit_valid, candidate_ok;
logic [44:0] candidate_command, commit_command;
logic [3:0] open_valid;
logic [15:0] open_rows;
{module['module_name']} dut (.clk(clk), .rst_n(rst_n), .candidate_valid(candidate_valid),
 .candidate_command(candidate_command), .commit_valid(commit_valid), .commit_command(commit_command),
 .open_valid(open_valid), .open_rows(open_rows), .candidate_ok(candidate_ok));
integer vectors, trace_fd, scan, op, amount, bank_arg, addr_arg, cycles_limit;
integer cycle=0;
logic [91:0] data_arg;
string vector_path, trace_path;
initial begin
 if (!$value$plusargs("VECTORS=%s",vector_path) || !$value$plusargs("TRACE=%s",trace_path)
     || !$value$plusargs("MAX_CYCLES=%d",cycles_limit)) $fatal(1,"Missing runner arguments");
 vectors=$fopen(vector_path,"r"); trace_fd=$fopen(trace_path,"w");
 if (vectors==0 || trace_fd==0) $fatal(1,"Cannot open simulation evidence");
 $fwrite(trace_fd,"cycle rst_n candidate_valid candidate_command commit_valid commit_command open_valid open_rows candidate_ok\\n");
 while (!$feof(vectors)) begin
  scan=$fscanf(vectors,"%d %d %d %d %h",op,amount,bank_arg,addr_arg,data_arg);
  if (scan==5) begin
   if ((op!=0 && op!=1) || amount<1) $fatal(1,"Invalid vector operation");
   repeat (amount) begin
    clk=0; rst_n=1'(op); open_valid=4'(bank_arg); open_rows=16'(addr_arg);
    candidate_command=data_arg[44:0]; commit_command=data_arg[89:45];
    candidate_valid=data_arg[90]; commit_valid=data_arg[91];
    #5;
    $fwrite(trace_fd,"%0h %0h %0h %0h %0h %0h %0h %0h %0h\\n",cycle,rst_n,candidate_valid,
       candidate_command,commit_valid,commit_command,open_valid,open_rows,candidate_ok);
    clk=1; #5; clk=0; cycle=cycle+1;
    if (cycle>=cycles_limit) $fatal(1,"SIMULATION_CYCLE_LIMIT %0d",cycle);
   end
  end else if (scan!=-1) $fatal(1,"Malformed vector row");
 end
 $fclose(trace_fd); $fclose(vectors);
 $display("SIMULATION_DONE cycles=%0d",cycle); $finish;
end
endmodule
'''


class TimingOracle:
    def __init__(self,shared):
        self.t=shared['timing_profile']['values'];self.acts=[None]*4;self.history=[];self.counts=Counter()

    def step(self,s):
        c=s['cycle'];op=(s['candidate_command']>>42)&7;b=(s['candidate_command']>>40)&3
        row=(s['candidate_command']>>36)&15;opened=bool(s['open_valid']&(1<<b))
        self.counts['edges']+=1
        expected=False
        if s['rst_n'] and s['candidate_valid']:
            self.counts['probes']+=1
            if op==1:
                recent=sum(c-e<self.t['tFAW'] for e in self.history)
                rrd=any(c-e<self.t['tRRD'] for e in self.history)
                expected=not opened and not rrd and recent<4
                if self.history:
                    age=c-self.history[-1]
                    for delta,label in ((-1,'T-1'),(0,'T'),(1,'T+1')):
                        self.counts['tRRD:'+label]+=int(age==self.t['tRRD']+delta)
                self.counts['tFAW:blocked_only']+=int(not opened and not rrd and recent>=4)
                expiry=any(c-e==self.t['tFAW'] for e in self.history)
                self.counts['tFAW:expiry_boundary']+=int(expiry and expected)
                self.counts['tFAW:expiry_with_commit']+=int(expiry and expected and s['commit_valid'] and ((s['commit_command']>>42)&7)==1)
            elif op in (2,3,4):
                timing='tRAS' if op==2 else 'tRCD'
                history=self.acts[b]
                expected=opened and (op==2 or (s['open_rows']>>(4*b))&15==row) and (
                    history is None or c-history>=self.t[timing])
                if history is not None:
                    for delta,label in ((-1,'T-1'),(0,'T'),(1,'T+1')):
                        self.counts[timing+':'+label]+=int(c-history==self.t[timing]+delta)
            self.counts['permitted' if expected else 'blocked']+=1
        if bool(s['candidate_ok'])!=bool(expected):
            raise CheckFailure('timing_permission',c,f'Expected candidate_ok={int(expected)}, got {s["candidate_ok"]}; opcode={op}, bank={b}')
        if not s['rst_n']:
            self.acts=[None]*4;self.history=[];self.counts['reset_edges']+=1
        elif s['commit_valid'] and ((s['commit_command']>>42)&7)==1:
            self.acts[(s['commit_command']>>40)&3]=c;self.history.append(c);self.counts['ACT_commits']+=1


def check_trace(path,shared,scenario):
    from simulate_handoff import read_trace
    if scenario not in CHECKS:raise ValueError('Unknown timing guard scenario')
    oracle=TimingOracle(shared);cycle=-1
    try:
        for row in read_trace(path):cycle=row['cycle'];oracle.step(row)
        expected_edges=sum(v[1] for v in scenarios(shared,1)[scenario])
        if cycle+1 != expected_edges:
            raise CheckFailure('scenario_coverage',cycle,f'Expected {expected_edges} edges, got {cycle+1}')
        required=['edges','probes','reset_edges']
        timer={'tRCD_per_bank':'tRCD','tRAS_per_bank':'tRAS','tRRD_global':'tRRD'}.get(scenario)
        if timer:required += [timer+':'+v for v in ('T-1','T','T+1')]
        if scenario=='tFAW_window':required += ['tFAW:blocked_only','tFAW:expiry_boundary','tFAW:expiry_with_commit']
        for key in required:
            if not oracle.counts[key]:raise CheckFailure('scenario_coverage',cycle,'Missing executed '+key)
        return {'status':'passed','cycles':cycle+1,'counters':dict(oracle.counts)}
    except CheckFailure as exc:
        return {'status':'failed','cycles':cycle+1,'counters':dict(oracle.counts),
                'failure':{'check':exc.check,'cycle':exc.cycle,'message':str(exc)}}
