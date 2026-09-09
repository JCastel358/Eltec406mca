"""Exercise actual app protocol parsers without hardware or firmware writes.

Derive the proposed sketch in memory from literal prepare_firmware.py changes.
Never run that generator, regenerate a sketch, connect serial, or flash a board.
Use --require-current-sketch after generation to make stale output a failure.
These tests establish host protocol compatibility, not MCU timing correctness.
"""
from __future__ import annotations

import argparse
import ast
from collections import deque
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
WORKSPACE=ROOT.parents[1]
GENERATOR=ROOT/"tools/prepare_firmware.py"
SOURCE=WORKSPACE/"Arduino/Eltec/Eltec.ino"
SKETCH=ROOT/"firmware/Eltec_USB_Master/Eltec_USB_Master.ino"
MODELS=("m405m22","m406mca","m449m18")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def derive_without_writes():
    original=SOURCE.read_text(encoding="utf-8")
    code=original
    count=0
    for node in ast.parse(GENERATOR.read_text(encoding="utf-8")).body:
        if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Name) and node.value.func.id=="replace":
            before,after=[ast.literal_eval(arg) for arg in node.value.args]
            matches=code.count(before)
            if matches!=1:
                raise AssertionError(f"Generator source anchor has {matches} matches: {before[:80]}")
            code=code.replace(before,after)
            count+=1
        elif isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=="code" for target in node.targets):
            value=node.value
            if isinstance(value,ast.BinOp) and isinstance(value.op,ast.Add) and isinstance(value.right,ast.Name) and value.right.id=="code":
                code=ast.literal_eval(value.left)+code
    if count==0:
        raise AssertionError("No reviewed literal firmware replacements found")
    return original,code,count


def load_backend(model):
    path=WORKSPACE/"single_detector_rig"/model/"esp32_backend.py"
    name="eltec_firmware_protocol_probe_"+model
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module,path


class FakeClock:
    def __init__(self):
        self.now=1.0

    def monotonic(self):
        self.now+=0.001
        return self.now

    def sleep(self,seconds):
        self.now+=seconds


def mock_transport(module,lines):
    clock=FakeClock()
    rig=module.Esp32Rig(monotonic=clock.monotonic,sleep=clock.sleep)
    remaining=deque(lines)
    sent=[]
    # Replace only the transport endpoints. Actual handshake, command parsing,
    # stop/drain control flow and stream-error handling are exercised unchanged.
    rig._send=lambda command:sent.append(command)
    rig._readline=lambda:remaining.popleft() if remaining else ""
    return rig,remaining,sent


def fixture_stream(module,lines):
    rig,remaining,sent=mock_transport(module,lines)
    rig._stream_active=True
    rig._stream_diagnostics=module.StreamDiagnostics(1000.0,"SENSOR",0.0)
    rig._stream_diagnostics.received_samples=24
    return rig,remaining,sent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-current-sketch",action="store_true")
    parser.add_argument("--output",type=Path,default=ROOT/"reports/firmware_protocol_checks.json")
    args=parser.parse_args()
    before_hashes={"source":sha(SOURCE),"generator":sha(GENERATOR),"generated_sketch":sha(SKETCH)}
    original,candidate,replacements=derive_without_writes()
    identity=re.search(r'Serial\.println\("(ELTEC-ESP32-ADS1256,v[^"\\]+)"\)',candidate).group(1)
    status_pattern=r'Serial\.printf\("(STATUS,[^"\n]+)"'
    old_status=re.search(status_pattern,original).group(1)
    new_status=re.search(status_pattern,candidate).group(1)
    matches=SKETCH.read_text(encoding="utf-8")==candidate
    checks=[]
    def check(name,work):
        try:
            detail=work()
            checks.append({"name":name,"passed":True,"detail":detail})
        except Exception as error:
            checks.append({"name":name,"passed":False,"error":f"{type(error).__name__}: {error}"})

    def status_unchanged():
        assert new_status==old_status
        return {"source_and_candidate_printf_format":new_status}
    check("status_wire_format_preserved",status_unchanged)
    if args.require_current_sketch:
        def current_sketch():
            assert matches,"Generated sketch does not match current generator; regenerate after active compilation finishes"
            return True
        check("generated_sketch_matches_current_generator",current_sketch)

    backend_sources={}
    fault="ERR,carrier power lost; capture invalid"
    for model in MODELS:
        module,path=load_backend(model)
        backend_sources[model]={"path":str(path.relative_to(WORKSPACE)),"sha256":sha(path)}
        def identity_test(module=module):
            parsed=module.Esp32Rig._validate_identity(identity,False)
            assert parsed.version==(3,3,0) and not parsed.ready_banner_seen
            return {"identity":parsed.text,"version":list(parsed.version),"identity_does_not_require_ready":True}
        check(model+"/identity_v3_3",identity_test)
        def handshake_test(module=module):
            rig,remaining,sent=mock_transport(module,["READY,ELTEC-ESP32-ADS1256",identity])
            parsed=rig._handshake()
            assert parsed.ready_banner_seen and parsed.text==identity and sent==["IDN?"] and not remaining
            return {"commands":sent,"ready_seen":True}
        check(model+"/normal_handshake",handshake_test)
        def missing_power_handshake(module=module):
            rig,remaining,sent=mock_transport(module,["ERR,carrier power or ADC not ready",identity])
            parsed=rig._handshake()
            assert parsed.text==identity and not parsed.ready_banner_seen
            return {"identity_accepted_with_adc_not_ready":True,"limitation":"This is serial identity only; no analog readiness claim."}
        check(model+"/identity_while_power_unavailable",missing_power_handshake)
        def status_test(module=module):
            line="STATUS,pwm=0,streaming=0,vref=2.500,rate=1000,pwm_hz=10.000,pwm_duty=50.0"
            rig,remaining,sent=mock_transport(module,[line])
            assert rig._command("STATUS?","STATUS,")==line and sent==["STATUS?"]
            return {"reply":line}
        check(model+"/status_reply",status_test)
        def old_order_reproduction(module=module):
            rig,remaining,sent=fixture_stream(module,["STREAM,END,24,0",fault])
            diagnostics=rig.stop_stream()
            assert diagnostics.stop_marker_seen and diagnostics.firmware_adc_overruns==0 and list(remaining)==[fault]
            return {"END_before_ERR_is_accepted":True,"fault_left_unconsumed":True,"purpose":"Reproduce why firmware must report captured faults before END."}
        check(model+"/old_END_then_ERR_failure_mode_reproduced",old_order_reproduction)
        def corrected_order(module=module):
            rig,remaining,sent=fixture_stream(module,[fault,"STREAM,END,24,1"])
            try:
                rig.stop_stream()
            except module.Esp32ProtocolError as error:
                assert not rig._stream_diagnostics.stop_marker_seen and list(remaining)==["STREAM,END,24,1"]
                return {"fault_rejected_before_END":True,"error":str(error)}
            raise AssertionError("Faulted stream termination was incorrectly accepted")
        check(model+"/ERR_before_END_rejects_capture",corrected_order)
        def scalar_error(module=module):
            rig,remaining,sent=mock_transport(module,["ERR,carrier power or ADC not ready; retry after ready"])
            try:
                rig._command("OFFSET?","OFFSET,")
            except module.Esp32ProtocolError as error:
                return {"invalid_scalar_rejected":True,"error":str(error)}
            raise AssertionError("Invalid OFFSET response was accepted")
        check(model+"/scalar_fault_rejected",scalar_error)

    after_hashes={"source":sha(SOURCE),"generator":sha(GENERATOR),"generated_sketch":sha(SKETCH)}
    def no_writes():
        assert before_hashes==after_hashes,"Input changed during check; rerun against a stable snapshot"
        return before_hashes
    check("firmware_inputs_unchanged",no_writes)
    failures=[item for item in checks if not item["passed"]]
    result={"status":"FAIL" if failures else "PASS","checks_passed":len(checks)-len(failures),"checks_failed":len(failures),
            "scope":"Real Python backend handshake/command/stop-stream parsers with mocked transport. No MCU execution, serial connection, compiler invocation, flashing, or generated firmware writes.",
            "inputs":before_hashes,"backend_sources":backend_sources,"literal_generator_replacements":replacements,
            "proposed_sketch_sha256":hashlib.sha256(candidate.encode()).hexdigest(),
            "generated_sketch_matches_current_generator":matches,
            "generated_sketch_note":"Current" if matches else "Pending regeneration; active compilation may still use the previous sketch. Tests above apply to prospective protocol and host parsing only.",
            "checks":checks}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":result["status"],"passed":result["checks_passed"],"failed":result["checks_failed"],"generated_sketch_matches_current_generator":matches,"report":str(args.output)}))
    if failures:
        raise SystemExit(1)


if __name__=="__main__":
    main()
