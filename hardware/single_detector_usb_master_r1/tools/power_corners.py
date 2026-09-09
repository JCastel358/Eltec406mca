"""Reproduce power-envelope arithmetic and check exported KiCad connections.

No SPICE or hardware validation is implied. The numerical margins are conditional
on the explicit measured/qualified acceptance inputs written into the report.
Run after build_design.py and validate_schematic.py have exported the netlist.
"""
from pathlib import Path
import argparse
import json
import math
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def calculate():
    setting = 51100 + 698
    setting_error = 0.001 + 10e-6 * 100
    regulated_low = 99e-6 * setting * (1-setting_error) - 0.002
    regulated_high = 101e-6 * setting * (1+setting_error) + 0.002
    # Exact Murata SET network: three 100nF C0G parts. IR is a resistance-
    # capacitance product, measured at room temperature after the stated test.
    # Use the weakest post-humidity 25 ohm.F bound, maximum cap tolerance and
    # post-test capacitance change, plus an explicit assembled-board leakage limit.
    cset_count, cset_each = 3, 100e-9
    cset_max = cset_count*cset_each*1.05*1.075
    cset_initial_min = cset_count*cset_each*0.95
    cset_posttest_min = cset_initial_min*0.925
    cset_leak = 5.25*cset_max/25
    board_set_leak = 25e-9
    setting_max = setting*(1+setting_error)
    regulated_low_with_leak = regulated_low-(cset_leak+board_set_leak)*setting_max
    regulated_high_with_leak = regulated_high+board_set_leak*setting_max
    divider_error = 0.001 + 25e-6 * 65
    rtmin, rtmax = 221*(1-divider_error), 221*(1+divider_error)
    rbmin, rbmax = 1000*(1-divider_error), 1000*(1+divider_error)
    kmin, kmax = 1+rtmin/rbmax, 1+rtmax/rbmin
    ref_error, comparator_error = 0.018+0.0012, 0.005+0.005
    trip_low = (4.096-ref_error-comparator_error)*kmin - 5e-6*rtmax
    trip_high = (4.096+ref_error+comparator_error)*kmax + 5e-6*rtmax
    overdrive_header = 0.050*kmax
    detect_low = trip_low-overdrive_header
    prior_high = trip_high+overdrive_header
    capacitance, current = 300e-6, 0.120
    module_loss, rail_step, response_budget = 0.100, 0.025, 10e-6
    avdd_permission = detect_low-module_loss-rail_step-current*response_budget/capacitance
    adc_cmax = 100e-9*1.2
    ground_tau = (220*1.01+17+100*1.01)*adc_cmax
    ground_to_02 = ground_tau*math.log(regulated_high_with_leak/0.2)
    signal_tau = (499*1.001+17+100*1.01)*adc_cmax
    lag = signal_tau*current/capacitance
    late_bleed = 2*3e-6*33000*1.001
    result = {
        "status": "conditional_design_envelope_not_bench_or_SPICE_validation",
        "acceptance_inputs_not_yet_verified": {
            "effective_held_capacitance_F_min": capacitance,
            "assembled_board_SET_leakage_A_max_each_direction": board_set_leak,
            "local_output_ceramic_C_F_min_at_actual_bias_frequency_temperature": 20e-6,
            "AP2112_local_bypass_C_F_min_each": 1e-6,
            "scope": "trial laboratory assembly; temperature-stressed silicon bounds do not establish a rig environmental rating",
            "SET_capacitor_IR_scope": "manufacturer room-temperature/post-test limits; assembled actual operating-temperature leakage must meet same budget",
            "held_discharge_current_A_max_including_LDO_recovery": current,
            "actual_AVDD_below_header_V_max": module_loss,
            "abrupt_header_step_V_max": rail_step,
            "aggregate_hardware_permission_response_s_max": response_budget,
            "module_AIN_capacitance_F_max_excluding_carrier_stray": adc_cmax,
            "actual_comparator_common_mode_and_load": "verify offset/delay at approximately4.1V common mode and actual gate load",
        },
        "regulated_V": {"nominal": setting*100e-6, "min": regulated_low, "max": regulated_high,
                        "min_with_SET_leakage_budget": regulated_low_with_leak,
                        "max_with_SET_leakage_budget": regulated_high_with_leak,
                        "margin_to_5V25_operating_limit": 5.25-regulated_high_with_leak},
        "comparator": {"divider_factor_min": kmin, "divider_factor_max": kmax,
                       "static_trip_header_V_min": trip_low, "static_trip_header_V_max": trip_high,
                       "header_V_for_50mV_prior_underdrive_max": prior_high,
                       "prior_underdrive_headroom_V": regulated_low_with_leak-prior_high,
                       "header_V_at_50mV_falling_overdrive_min": detect_low,
                       "actual_AVDD_V_when_permission_must_drop_min": avdd_permission,
                       "operating_margin_at_permission_drop_V": avdd_permission-4.75},
        "analog": {"ground_path_tau_s_max": ground_tau, "ground_to_0V2_s_max": ground_to_02,
                   "signal_tracking_tau_s_max": signal_tau, "declining_rail_tracking_lag_V_max": lag,
                   "conditional_AIN_minus_AVDD_V_max_before_ground": lag+rail_step+module_loss,
                   "powered_off_33k_bleed_V_max_before_temperature_drift": late_bleed},
        "exact_passive_closure": {
            "C10_TPSD226K025R0200": {
                "nominal_F":22e-6,"initial_min_F":22e-6*0.9,
                "illustrative_min_after_10percent_change_F":22e-6*0.9*0.9,
                "DC_bias_derating":False,"maximum_ESR_100kHz_ohm":0.2,
                "parallel_input_ceramic":"C36 GRM32ER71E226KE15L22uF25VX7R1210",
                "limit":"battery lead resonance/hot-plug current and damping require actual wiring validation",
            },
            "C11_C13_C39_GRM32ER71E226KE15L": {
                "count":3,"nominal_F_each":22e-6,
                "illustrative_small_signal_bias_temp_tol_F_total":3*22e-6*0.8*0.7*0.9*0.85,
                "required_effective_F_total":20e-6,
                "required_ESR_ohm_max_each":0.020,"required_mounted_ESL_H_max_each":2e-9,
                "limit":"0.8DC and0.7smallAC factors are separate typical manufacturer curves, not a combined-condition guarantee; verify>=20uF and layout/stability",
            },
            "C15_C37_C38_GRM31C5C1H104JA01K": {
                "nominal_F_total":cset_count*cset_each,
                "initial_F_min":cset_initial_min,"post_humidity_F_min_envelope":cset_posttest_min,
                "room_IR_ohm_F_min":500,"post_durability_IR_ohm_F_min":50,
                "post_humidity_IR_ohm_F_min":25,"leak_A_max_used":cset_leak,
                "nominal_RC_corner_Hz":1/(2*math.pi*setting*cset_count*cset_each),
                "nominal_t90_s":math.log(10)*setting*cset_count*cset_each,
                "nominal_initial_slew_V_per_s":100e-6/(cset_count*cset_each),
                "corner_initial_slew_V_per_s":101e-6/cset_posttest_min,
                "startup_total_output_C_F_envelope":1.10e-3,
                "corner_charging_plus120mA_A":101e-6/cset_posttest_min*1.10e-3+0.120,
                "limit":"startup can enter the nominal500mA programmed current limit; native ready qualification prevents conversion until settled. No4.7uF noise claim; validate inrush/thermal and release overshoot",
            },
            "C14_C23_C24": {
                "mpn":"GRM31C5C1H104JA01K","footprint":"1206",
                "nominal_F_each":100e-9,"tolerance":0.05,
                "C24_qualification_s_initial_min":350e3*100e-9*0.95*math.log(1/0.36),
                "C24_qualification_s_posthumidity_min_envelope":350e3*100e-9*0.95*0.925*math.log(1/0.36),
            },
            "R40_Q26_release": {
                "resistance_ohm":150,"peak_A_max":5.25/(150*0.99),
                "nominal_RC_s":150*100e-9,
                "effect":"startup-only clamp release; independent comparator andQ24 shutdown unaffected",
            },
            "D12_Diotec_BZT52C6V8GW": {
                "Vz_5mA_25C_min_V":6.4,"Vz_5mA_25C_max_V":7.2,
                "tempco_max_per_K":0.0007,"illustrative125C_max_V":7.2*(1+0.0007*100),
                "AO3414_absolute_VGS_V":8,
                "limit":"Zener limits specified at5mA; R13 restricts normal current far below this. Verify transient clamp with actual battery wiring",
            },
        },
        "C12_Nichicon_PCJ0J821MCL4GS": {
            "nominal_F": 820e-6, "initial_20C_min_F": 820e-6*0.8,
            "after_endurance_20C_min_F": 820e-6*0.8*0.8,
            "ESR_20C_100kHz_max_ohm": 0.010,
            "ESR_endurance_limit_factor": 1.5,
            "impedance_temperature_ratio_100kHz_max": 1.25,
            "illustrative_120mA_step_with_1p5x1p25_ESR_V": 0.12*0.010*1.5*1.25,
            "limit": "100kHz impedance/endurance specs are not a broadband transient or low-temperature capacitance guarantee; qualified300uF/25mV envelope still required",
        },
    }
    assert regulated_low_with_leak > prior_high
    assert regulated_high_with_leak < 5.25
    assert avdd_permission > 4.75
    assert lag+rail_step+module_loss < 0.3
    assert late_bleed < 0.3
    return result


def check_netlist(path):
    tree = ET.parse(path)
    connected = {}
    full_names = {}
    for net in tree.findall("./nets/net"):
        for node in net.findall("node"):
            connected[(node.get("ref"), node.get("pin"))] = net.get("name").split("/")[-1]
            full_names[(node.get("ref"), node.get("pin"))] = net.get("name")
    expected = {
        "U10": {1:"DET_BAT_PROTECTED",2:"DET_BAT_PROTECTED",3:"DET_BAT_PROTECTED",5:"ADC_REG_EN",
                7:"ADC_LDO_ILIM",8:"DET_BAT_PROTECTED",9:"ADC_LDO_SET",10:"GND",11:"GND",
                12:"ADC_5V_HELD",13:"ADC_5V_HELD",14:"ADC_5V_HELD",15:"GND"},
        "U11": {1:"ADC_5V_HELD",2:"GND",3:"ADC_5V_HELD",5:"ADC_IO_3V3"},
        "U18": {1:"ADC_COMPARATOR_GOOD",2:"GND",3:"ADC_RAIL_SENSE",4:"PWR_REF_4V096",5:"ADC_5V_HELD"},
        "U19": {1:"ADC_READY_RAW",2:"REF_READY_RAW",3:"GND",4:"ESP_POWER_NOT_READY",5:"ESP_3V3"},
        "Q14": {1:"ADC_READY_RAW",2:"GND",3:"ISOLATE_H"},
        "Q23": {1:"REF_NOT_READY",2:"GND",3:"ADC_READY_RAW"},
        "Q24": {1:"POWER_OFF_NODE",2:"GND",3:"ADC_READY_RAW"},
        "Q26": {1:"REF_READY_RAW",2:"GND",3:"REF_CLAMP_RELEASE"},
        "R40": {1:"REF_NOT_READY",2:"REF_CLAMP_RELEASE"},
        "Q17": {1:"DET_MASTER_GATE",2:"DET_BAT_PROTECTED",3:"SENSOR_BAT_SW"},
        "Q20": {1:"EM_MASTER_GATE",2:"EM_BAT_PROTECTED",3:"EM_BAT_SW"},
        "U16": {1:"EM_MASTER_LED_A",2:"EM_MASTER_LED_K",3:"EM_GND",4:"EM_OPTO_C"},
        "R30": {1:"ADC_5V_HELD",2:"PWR_REF_4V096"},
        "C10": {1:"DET_BAT_PROTECTED",2:"GND"},
        "C36": {1:"DET_BAT_PROTECTED",2:"GND"},
        "C11": {1:"ADC_5V_HELD",2:"GND"},
        "C12": {1:"ADC_5V_HELD",2:"GND"},
        "C13": {1:"ADC_5V_HELD",2:"GND"},
        "C39": {1:"ADC_5V_HELD",2:"GND"},
        "C15": {1:"ADC_LDO_SET",2:"GND"},
        "C37": {1:"ADC_LDO_SET",2:"GND"},
        "C38": {1:"ADC_LDO_SET",2:"GND"},
        "C23": {1:"ADC_5V_HELD",2:"REF_NOT_READY"},
        "R17": {1:"ADC_5V_HELD",2:"ISOLATE_H"},
        "R19": {1:"MASTER_PERMIT",2:"SPI_PERMIT"},
        "Q22": {1:"EM_LED_ENABLE_GATE",2:"GND",3:"EM_LED_RETURN"},
        "U1": {1:"ESP_3V3",16:"USB_PRESENT_5V",26:"ESP_POWER_NOT_READY"},
    }
    failures=[]
    for ref,pins in expected.items():
        for pin,net in pins.items():
            actual=connected.get((ref,str(pin)))
            if actual != net: failures.append({"ref":ref,"pin":pin,"expected":net,"actual":actual})
    for pin in [4,6]:
        if ("U10",str(pin)) in connected and not full_names[("U10",str(pin))].startswith("unconnected-"):
            failures.append({"ref":"U10","pin":pin,"expected":"unconnected","actual":connected[("U10",str(pin))]})
    return {"source":str(path), "checked_pin_connections":sum(len(p) for p in expected.values()),
            "failures":failures,"passed":not failures}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--netlist",type=Path,default=ROOT/"reports/interface_netlist.xml")
    parser.add_argument("--skip-netlist",action="store_true")
    parser.add_argument("--output",type=Path,default=ROOT/"reports/power_corners.json")
    args=parser.parse_args()
    result=calculate()
    if not args.skip_netlist: result["exported_netlist_checks"]=check_netlist(args.netlist)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))
    if not args.skip_netlist and not result["exported_netlist_checks"]["passed"]: raise SystemExit(1)

if __name__=="__main__": main()
