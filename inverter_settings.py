"""The inverter's settings (holding registers) as EG4 Monitor's "Remote Set" page shows them, with plain explanations.

Register map for EG4's off-grid family (6000XP / 12000XP, device type 54 or 38). Sources:
  - pylxpweb's live probe of an SNA12K-US (the 12000XP) through EG4's own remoteRead, which names every register
    (docs/inverters/SNA12KUS_52XXXXXX68.json): https://github.com/joyfulhouse/pylxpweb (MIT)
  - pylxpweb's holding-register map (registers/inverter_holding.py, constants/registers.py) for bit positions and units
  - this project's own findings on the owner's 6000XP (local.py: window #3 at 72/73, charge type in 120 bits 1-3, end voltage 159)

Nothing here is trusted blindly: each setting starts locked, and is unlocked for writing only after `compare()` finds that
EG4's own decoding of the owner's inverter (remoteRead) gives the same value as this map's decoding of the raw register,
or when it was verified on the owner's inverter (`verified`). See the "Inverter settings" notes in CLAUDE.md.
"""
from dataclasses import dataclass, field
import re


@dataclass(frozen=True)
class Setting:
    key: str              # unique id; EG4's own parameter name where it has one (what remoteRead returns)
    reg: int              # holding register (a window uses reg and reg + 1)
    label: str
    group: str
    help: str
    kind: str = "number"  # number | switch | select | window
    bit: int | None = None
    width: int = 1        # bits, for a switch or a select inside a shared register
    div: int = 1          # shown value = raw / div
    signed: bool = False
    unit: str = ""
    lo: float | None = None
    hi: float | None = None
    options: dict = field(default_factory=dict)   # select: raw value -> label
    risk: str = ""        # "" | "caution" | "danger"
    writable: bool = True
    verified: bool = False   # its register was verified on the owner's 6000XP (writable without the EG4 check)
    cloud: tuple = ()     # EG4 parameter names that hold its value (default: (key,)); a window has 4 (start h/m, end h/m)

    @property
    def regs(self):
        return (self.reg, self.reg + 1) if self.kind == "window" else (self.reg,)

    @property
    def cloud_keys(self):
        return self.cloud or (self.key,)

    @property
    def step(self):
        return 1 / self.div


GROUPS = [
    ("charge", "Grid charging", "When and how hard the inverter charges the battery from the grid (EG4 calls it AC charge)."),
    ("battery", "Battery charging", "How full the battery is charged, how fast, and at what voltage."),
    ("discharge", "Battery use and cutoffs", "How low the battery may go before the inverter stops using it, with and without the grid."),
    ("output", "Working mode and output", "Where the house gets its power first, and how the inverter treats the grid input."),
    ("windows", "Time windows", "Daily windows for solar-charges-first, forced battery use and grid-first."),
    ("generator", "Generator", "Charging from a generator on the GEN port."),
    ("smartload", "Smart load and AC coupling", "Other uses of the GEN port: a switched load, or an existing grid-tie solar inverter."),
    ("functions", "Other functions", "Beeper, safety checks and calibration."),
    ("grid", "Grid connection and protection", "Installer settings: when the inverter accepts the grid, and when it disconnects. "
                                               "Wrong values can make it drop the grid or refuse it entirely."),
    ("info", "About this inverter", "Fixed at installation; shown for reference."),
]

UNDOC = " EG4 doesn't document exactly what it does; leave it as it is unless EG4 support tells you otherwise."
BAT_PCT = {0: "Battery %", 1: "Voltage"}


def _window(prefix, base, n, group, label, help, suffixes=("", "_1", "_2"), **kw):
    """Daily time windows: each is two registers, start and end, packed as hour | minute << 8."""
    out = []
    for i, suf in enumerate(suffixes[:n]):
        out.append(Setting(f"{prefix}_WINDOW_{i + 1}", base + 2 * i, f"{label} {i + 1}", group, help, kind="window",
                           cloud=(f"{prefix}_START_HOUR{suf}", f"{prefix}_START_MINUTE{suf}",
                                  f"{prefix}_END_HOUR{suf}", f"{prefix}_END_MINUTE{suf}"), **kw))
    return out


def _sw(key, reg, bit, label, group, help, **kw):
    return Setting(key, reg, label, group, help, kind="switch", bit=bit, **kw)


V = dict(unit="V", div=10)
SETTINGS = [
    # ------------------------------------------------------------------ grid charging
    _sw("FUNC_AC_CHARGE", 21, 7, "Charge the battery from the grid", "charge",
        "Lets the inverter charge the battery from utility power. When it's off, the battery charges from solar only. "
        "The settings below say when grid charging runs.", risk="caution"),
    Setting("BIT_AC_CHARGE_TYPE", 120, "Grid charging is based on", "charge",
            "What decides when grid charging runs. Time: it charges inside the windows below. Battery level: it starts when "
            "the battery falls to the start level and stops at the stop level, at any time of day. Both: only inside a window, "
            "and only while the battery is below the stop level. EG4's Quick Charge switches this to battery level while it "
            "runs and back afterwards.",
            kind="select", bit=1, width=3, options={0: "Time windows", 1: "Battery level", 2: "Time windows and battery level"},
            verified=True),
    *_window("HOLD_AC_CHARGE", 68, 3, "charge", "Grid charge window",
             "A daily time range when grid charging may run (used when grid charging is based on time). Start and end both at "
             "midnight (00:00, or 12:00 AM) means the window is off. A window may cross midnight (10 PM to 6 AM). EG4's Quick Charge uses window 3, so a "
             "running Quick Charge shows up there.", verified=True),
    Setting("HOLD_AC_CHARGE_POWER_CMD", 66, "Grid charge power", "charge",
            "The most power the inverter draws from the grid to charge the battery. Lower it if grid charging trips a "
            "breaker or overloads a generator. The charge current limit also applies.",
            unit="kW", div=10, lo=0, hi=12),
    Setting("HOLD_AC_CHARGE_SOC_LIMIT", 67, "Stop time-window charging at", "charge",
            "In time-window mode, grid charging stops early once the battery reaches this level. 101 means don't stop early.",
            unit="%", lo=0, hi=101),
    Setting("HOLD_AC_CHARGE_START_BATTERY_SOC", 160, "Start grid charging at (battery %)", "charge",
            "In battery-level mode, grid charging starts when the battery falls to this level.", unit="%", lo=0, hi=90),
    Setting("HOLD_AC_CHARGE_END_BATTERY_SOC", 161, "Stop grid charging at (battery %)", "charge",
            "In battery-level mode, grid charging stops when the battery reaches this level.", unit="%", lo=20, hi=100),
    Setting("HOLD_AC_CHARGE_START_BATTERY_VOLTAGE", 158, "Start grid charging at (voltage)", "charge",
            "The same start point as a battery voltage, used when the inverter works by voltage (no battery % from a BMS, "
            "or charge limits set to voltage).", lo=38.4, hi=52, **V),
    Setting("HOLD_AC_CHARGE_END_BATTERY_VOLTAGE", 159, "Stop grid charging at (voltage)", "charge",
            "The same stop point as a battery voltage. EG4's Quick Charge raises it to 59.0 V while it runs and puts it back "
            "afterwards (54.0 V on this inverter).", lo=48, hi=59, verified=True, **V),
    Setting("HOLD_AC_CHARGE_BATTERY_CURRENT", 168, "Grid charge current limit", "charge",
            "The most current grid charging puts into the battery. Your battery's maximum charge current is the upper bound.",
            unit="A", lo=0, hi=140),
    _sw("FUNC_HALF_HOUR_AC_CHG_START_EN", 120, 0, "Half-hour grid charge start", "charge", "EG4's “Half hour AC charge start”." + UNDOC),

    # ------------------------------------------------------------------ battery charging
    Setting("HOLD_LEAD_ACID_CHARGE_VOLT_REF", 99, "Charge voltage", "battery",
            "The voltage the inverter charges the battery up to (bulk/absorb). With a lithium battery whose BMS talks to the "
            "inverter, the BMS normally sets this and this value is the fallback. Use your battery maker's figure: too high "
            "can trip the battery's protection.", lo=50, hi=59, risk="caution", **V),
    Setting("HOLD_FLOATING_VOLTAGE", 144, "Float voltage", "battery",
            "The lower voltage held once the battery is full, to keep it topped up. Mostly matters for lead-acid batteries.",
            lo=50, hi=56, risk="caution", **V),
    Setting("HOLD_LEAD_ACID_CHARGE_RATE", 101, "Charge current limit", "battery",
            "The most current going into the battery from solar and grid together. Set it at or below your battery bank's "
            "maximum charge current.", unit="A", lo=0, hi=140, risk="caution"),
    Setting("HOLD_CHARGE_POWER_PERCENT_CMD", 64, "Charge power limit", "battery",
            "Caps battery charging as a percentage of the inverter's maximum. 100% means no extra cap.", unit="%", lo=0, hi=100),
    Setting("HOLD_SYSTEM_CHARGE_SOC_LIMIT", 227, "Stop all charging at (battery %)", "battery",
            "Charging from every source stops at this level. 101 lets the battery reach 100% and balance its cells; a lower "
            "value such as 90% is gentler on lithium batteries but leaves less in reserve for an outage.", unit="%", lo=0, hi=101),
    Setting("HOLD_SYSTEM_CHARGE_VOLT_LIMIT", 228, "Stop all charging at (voltage)", "battery",
            "The same overall stop point as a voltage, used when charge limits are set to voltage.", lo=48, hi=60, **V),
    Setting("FUNC_BAT_CHARGE_CONTROL", 179, "Charge limits use", "battery",
            "Whether charging stops by battery % (from the battery's BMS) or by voltage. Use battery % with a lithium battery "
            "connected to the inverter's BMS port; voltage with lead-acid or a battery that doesn't report its charge.",
            kind="select", bit=9, options=BAT_PCT),
    Setting("HOLD_EQUALIZATION_VOLTAGE", 149, "Equalization voltage", "battery",
            "Lead-acid only: an occasional overcharge that evens out the cells. Never equalize a lithium battery.",
            lo=50, hi=59, risk="caution", **V),
    Setting("HOLD_EQUALIZATION_PERIOD", 150, "Equalize every", "battery", "Lead-acid only: days between equalization charges.",
            unit="days", lo=0, hi=365, risk="caution"),
    Setting("HOLD_EQUALIZATION_TIME", 151, "Equalization time", "battery", "Lead-acid only: how long each equalization lasts. 0 turns it off.",
            unit="h", lo=0, hi=24, risk="caution"),
    Setting("HOLD_LEAD_ACID_TEMPR_LOWER_LIMIT_CHG", 108, "Lowest temperature to charge", "battery",
            "Charging stops below this battery temperature (used when the battery's BMS doesn't report it).",
            unit="°C", signed=True, lo=-30, hi=20, risk="caution"),
    Setting("HOLD_LEAD_ACID_TEMPR_UPPER_LIMIT_CHG", 109, "Highest temperature to charge", "battery",
            "Charging stops above this battery temperature.", unit="°C", signed=True, lo=20, hi=70, risk="caution"),

    # ------------------------------------------------------------------ battery use and cutoffs
    Setting("HOLD_SOC_LOW_LIMIT_EPS_DISCHG", 125, "Off-grid cutoff (battery %)", "discharge",
            "With no grid, the inverter turns its output off when the battery falls to this level, to protect the battery. "
            "Lower gives you more hours in an outage; too low risks the battery shutting itself down.", unit="%", lo=0, hi=100,
            risk="caution"),
    Setting("HOLD_LEAD_ACID_DISCHARGE_CUT_OFF_VOLT", 100, "Off-grid cutoff (voltage)", "discharge",
            "The same off-grid cutoff as a battery voltage, used when cutoffs are set to voltage.", lo=40, hi=50, risk="caution", **V),
    Setting("HOLD_DISCHG_CUT_OFF_SOC_EOD", 105, "On-grid cutoff (battery %)", "discharge",
            "With the grid available, the inverter stops using the battery at this level and runs the house from the grid, "
            "keeping the rest for outages. Raise it to keep more in reserve.", unit="%", lo=10, hi=90),
    Setting("HOLD_ON_GRID_EOD_VOLTAGE", 169, "On-grid cutoff (voltage)", "discharge",
            "The same on-grid cutoff as a battery voltage.", lo=40, hi=56, **V),
    Setting("FUNC_BAT_DISCHARGE_CONTROL", 179, "Cutoffs use", "discharge",
            "Whether the cutoffs above go by battery % or by voltage.", kind="select", bit=10, options=BAT_PCT),
    Setting("BIT_ON_GRID_EOD_TYPE", 120, "On-grid cutoff uses", "discharge",
            "Whether the on-grid cutoff goes by voltage or battery %.", kind="select", bit=6, options={0: "Voltage", 1: "Battery %"}),
    Setting("BIT_DISCHG_CONTROL_TYPE", 120, "Discharge control type", "discharge", "EG4's discharge control type selector." + UNDOC,
            kind="select", bit=4, width=2, options={0: "Type 0", 1: "Type 1", 2: "Type 2", 3: "Type 3"}, risk="caution"),
    Setting("HOLD_BATTERY_LOW_TO_UTILITY_SOC", 167, "Switch the house to grid at (battery %)", "discharge",
            "With battery-first output, the house switches over to grid power when the battery falls to this level, and back "
            "to the battery once it has recharged.", unit="%", lo=0, hi=100),
    Setting("HOLD_BATTERY_LOW_TO_UTILITY_VOLTAGE", 166, "Switch the house to grid at (voltage)", "discharge",
            "The same switch-over point as a battery voltage.", lo=0, hi=52, **V),
    Setting("HOLD_BATTERY_WARNING_SOC", 164, "Low battery warning (battery %)", "discharge",
            "The inverter raises a low-battery warning at this level.", unit="%", lo=0, hi=90),
    Setting("HOLD_BATTERY_WARNING_RECOVERY_SOC", 165, "Warning clears at (battery %)", "discharge",
            "The low-battery warning clears once the battery is back above this level.", unit="%", lo=0, hi=100),
    Setting("HOLD_BATTERY_WARNING_VOLTAGE", 162, "Low battery warning (voltage)", "discharge",
            "The low-battery warning as a battery voltage.", lo=0, hi=50, **V),
    Setting("HOLD_BATTERY_WARNING_RECOVERY_VOLTAGE", 163, "Warning clears at (voltage)", "discharge",
            "The low-battery warning clears above this battery voltage.", lo=0, hi=52, **V),
    Setting("HOLD_LEAD_ACID_DISCHARGE_RATE", 102, "Discharge current limit", "discharge",
            "The most current the inverter takes from the battery. Keep it at or below your battery bank's rating.",
            unit="A", lo=0, hi=140, risk="caution"),
    Setting("HOLD_DISCHG_POWER_PERCENT_CMD", 65, "Discharge power limit", "discharge",
            "Caps battery output as a percentage of the inverter's maximum. 100% means no extra cap.", unit="%", lo=0, hi=100),
    Setting("HOLD_P_TO_USER_START_DISCHG", 116, "Start the battery above this house load", "discharge",
            "The battery only helps once the house draws more than this from the grid. Mostly a grid-tied-hybrid setting.",
            unit="W", lo=0, hi=10000),
    Setting("HOLD_LEAD_ACID_TEMPR_LOWER_LIMIT_DISCHG", 106, "Lowest temperature to discharge", "discharge",
            "Battery use stops below this battery temperature (used when the BMS doesn't report it).",
            unit="°C", signed=True, lo=-30, hi=20, risk="caution"),
    Setting("HOLD_LEAD_ACID_TEMPR_UPPER_LIMIT_DISCHG", 107, "Highest temperature to discharge", "discharge",
            "Battery use stops above this battery temperature.", unit="°C", signed=True, lo=20, hi=70, risk="caution"),

    # ------------------------------------------------------------------ working mode and output
    Setting("HOLD_OUTPUT_CONFIGURATION", 145, "Output priority", "output",
            "Where the house gets power first. Battery first: solar, then the battery, and the grid only once the battery is "
            "low (most self-powered). Solar first: solar, then the grid, keeping the battery for outages. Grid first: the grid "
            "runs the house and solar mostly charges the battery.",
            kind="select", options={0: "Battery first", 1: "Solar first", 2: "Grid first"}),
    Setting("HOLD_LINE_MODE_INPUT", 146, "AC input range", "output",
            "How fussy the inverter is about grid or generator power before using it. UPS: narrow voltage range and the "
            "fastest switch-over, so computers stay on. Appliance (APL): wider range, fine for most homes. Generator: widest "
            "range, tolerant of a generator's wandering voltage and frequency.",
            kind="select", options={0: "Appliance (APL)", 1: "UPS", 2: "Generator"}),
    Setting("HOLD_MAX_AC_INPUT_POWER", 176, "Grid input power limit", "output",
            "The most power drawn from the grid input. When the house plus charging needs more, charging is cut back first "
            "and the battery helps cover the house.", unit="W", lo=0, hi=12000),
    _sw("FUNC_SET_TO_STANDBY", 21, 9, "Inverter on (off = standby)", "output",
        "Turning this off puts the inverter in standby: its AC output switches OFF and everything it powers loses power "
        "until it's turned back on.", risk="danger"),
    _sw("FUNC_EPS_EN", 21, 0, "Backup output (EPS)", "output",
        "The inverter's battery-backed output. On the 6000XP this feeds the house: turning it off can cut power during a "
        "grid outage.", risk="danger"),
    _sw("FUNC_SW_SEAMLESSLY_EN", 21, 8, "Seamless switching", "output",
        "Switches between grid and battery without a gap, so clocks and computers don't notice an outage.", risk="caution"),
    _sw("FUNC_GREEN_EN", 110, 14, "Green mode", "output",
        "Runs on solar and battery as much as possible, using the grid only when they can't keep up."),
    _sw("FUNC_CHARGE_LAST", 110, 4, "Charge the battery last", "output",
        "Solar powers the house first, and only what's left over charges the battery."),
    _sw("FUNC_BATTERY_ECO_EN", 110, 15, "Battery eco mode", "output",
        "Cycles the battery less to make it last longer, at the cost of using a little more grid power."),
    _sw("FUNC_ON_GRID_ALWAYS_ON", 179, 15, "Grid always on", "output", "Keeps the grid connection closed at all times." + UNDOC),
    _sw("FUNC_PV_GRID_OFF_EN", 110, 0, "PV grid-off", "output", "EG4's “PV grid off” switch." + UNDOC),

    # ------------------------------------------------------------------ time windows
    _sw("FUNC_FORCED_CHG_EN", 21, 11, "Solar charges the battery first", "windows",
        "During the charge-first windows below, solar charges the battery before powering the house (EG4: “PV charge "
        "priority” or “Charge first”). Handy before an expected outage."),
    Setting("HOLD_FORCED_CHG_POWER_CMD", 74, "Charge-first power", "windows", "The charge power used during the charge-first windows.",
            unit="kW", div=10, lo=0, hi=12),
    Setting("HOLD_FORCED_CHG_SOC_LIMIT", 75, "Charge-first stops at", "windows",
            "Charge-first ends early once the battery reaches this level.", unit="%", lo=0, hi=100),
    *_window("HOLD_FORCED_CHARGE", 76, 3, "windows", "Charge-first window",
             "A daily time range when solar charges the battery before the house (needs “Solar charges the battery first” on). "
             "Start and end both at midnight (12:00 AM) means off."),
    _sw("FUNC_FORCED_DISCHG_EN", 21, 10, "Forced battery use", "windows",
        "During the forced windows below, the house runs from the battery even when solar or the grid could cover it, down "
        "to the stop level. Useful for using stored energy at times of day when grid power costs more."),
    Setting("HOLD_FORCED_DISCHG_POWER_CMD", 82, "Forced battery power", "windows", "The battery power used during the forced windows.",
            unit="kW", div=10, lo=0, hi=12),
    Setting("HOLD_FORCED_DISCHG_SOC_LIMIT", 83, "Forced battery use stops at", "windows",
            "Forced battery use ends once the battery falls to this level.", unit="%", lo=0, hi=100),
    *_window("HOLD_FORCED_DISCHARGE", 84, 3, "windows", "Forced battery window",
             "A daily time range for forced battery use (needs “Forced battery use” on). Start and end both at midnight (12:00 AM) means off."),
    *_window("HOLD_AC_FIRST", 152, 3, "windows", "Grid-first window",
             "A daily time range when the house runs on grid power and the battery is saved, whatever the output priority. "
             "Start and end both at midnight (12:00 AM) means off."),

    # ------------------------------------------------------------------ generator
    Setting("BIT_GENERATOR_CHARGE_TYPE", 120, "Generator charging is based on", "generator",
            "Whether generator charging starts and stops by battery voltage or battery %.", kind="select", bit=7,
            options={0: "Voltage", 1: "Battery %"}),
    Setting("OFF_GRID_HOLD_GEN_CHG_START_SOC", 196, "Start generator charging at (battery %)", "generator",
            "Charging from the generator starts when the battery falls to this level.", unit="%", lo=0, hi=90),
    Setting("OFF_GRID_HOLD_GEN_CHG_END_SOC", 197, "Stop generator charging at (battery %)", "generator",
            "Charging from the generator stops at this level.", unit="%", lo=20, hi=100),
    Setting("OFF_GRID_HOLD_GEN_CHG_START_VOLT", 194, "Start generator charging at (voltage)", "generator",
            "The generator start point as a battery voltage.", lo=38.4, hi=52, **V),
    Setting("OFF_GRID_HOLD_GEN_CHG_END_VOLT", 195, "Stop generator charging at (voltage)", "generator",
            "The generator stop point as a battery voltage.", lo=48, hi=59, **V),
    Setting("OFF_GRID_HOLD_MAX_GEN_CHG_BAT_CURR", 198, "Generator charge current limit", "generator",
            "The most current generator charging puts into the battery. Lower it for a small generator.", unit="A", lo=0, hi=140),
    Setting("HOLD_MAX_GENERATOR_INPUT_POWER", 177, "Generator rated power", "generator",
            "Your generator's continuous rating. The inverter keeps its draw under this, cutting charging back first.",
            unit="W", lo=0, hi=12000),
    _sw("FUNC_GEN_PEAK_SHAVING", 179, 8, "Generator peak shaving", "generator",
        "When the house needs more than the generator's rating, the battery covers the difference instead of overloading it."),
    *_window("HOLD_GEN", 256, 2, "generator", "Generator charge window",
             "A daily time range when generator charging may run. Start and end both at midnight (12:00 AM) means off.", suffixes=("_1", "_2")),

    # ------------------------------------------------------------------ smart load and AC coupling
    Setting("FUNC_SMART_LOAD_ENABLE", 179, "GEN port is used for", "smartload",
            "The GEN port can take a generator, or work as a smart load output that powers one circuit (a water heater, a "
            "pool pump) only when there's energy to spare.", kind="select", bit=13,
            options={0: "Generator", 1: "Smart load"}, risk="caution"),
    Setting("_12K_HOLD_SMART_LOAD_START_SOC", 215, "Smart load on at (battery %)", "smartload",
            "The smart load output switches on once the battery is above this level.", unit="%", lo=0, hi=100),
    Setting("_12K_HOLD_SMART_LOAD_END_SOC", 216, "Smart load off at (battery %)", "smartload",
            "The smart load output switches off when the battery falls to this level.", unit="%", lo=0, hi=100),
    Setting("_12K_HOLD_SMART_LOAD_START_VOLT", 213, "Smart load on at (voltage)", "smartload",
            "The switch-on point as a battery voltage.", lo=40, hi=60, **V),
    Setting("_12K_HOLD_SMART_LOAD_END_VOLT", 214, "Smart load off at (voltage)", "smartload",
            "The switch-off point as a battery voltage.", lo=40, hi=60, **V),
    _sw("FUNC_AC_COUPLING_FUNCTION", 179, 11, "AC coupling", "smartload",
        "For an existing grid-tie solar inverter wired to the GEN port: the 6000XP uses its power and shuts it down (by "
        "nudging the frequency) when the battery is full.", risk="caution"),
    Setting("_12K_HOLD_AC_COUPLE_START_SOC", 220, "AC-coupled solar back on at (battery %)", "smartload",
            "The AC-coupled inverter is allowed to run again once the battery falls to this level.", unit="%", lo=0, hi=100),
    Setting("_12K_HOLD_AC_COUPLE_END_SOC", 221, "AC-coupled solar off at (battery %)", "smartload",
            "The AC-coupled inverter is shut down when the battery reaches this level.", unit="%", lo=0, hi=100),
    Setting("_12K_HOLD_AC_COUPLE_START_VOLT", 222, "AC-coupled solar back on at (voltage)", "smartload",
            "The same restart point as a battery voltage.", lo=40, hi=60, **V),
    Setting("_12K_HOLD_AC_COUPLE_END_VOLT", 223, "AC-coupled solar off at (voltage)", "smartload",
            "The same shut-down point as a battery voltage.", lo=40, hi=60, **V),

    # ------------------------------------------------------------------ other functions
    _sw("FUNC_BUZZER_EN", 110, 7, "Alarm beeper", "functions", "The inverter beeps on warnings and faults."),
    _sw("FUNC_PV_ARC", 179, 12, "Solar arc-fault detection", "functions",
        "Watches the solar wiring for arcing (a fire risk) and shuts solar down if it finds any. Usually required by code; "
        "keep it on unless nuisance trips are confirmed.", risk="caution"),
    _sw("FUNC_RSD_DISABLE", 179, 14, "Rapid shutdown disabled", "functions",
        "On means rapid shutdown is DISABLED. Rapid shutdown de-energizes roof panels for firefighters and is required by "
        "code for most roof arrays.", risk="danger"),
    _sw("FUNC_ISO_EN", 21, 12, "Solar insulation check", "functions",
        "Checks the solar wiring's insulation to ground before starting solar, and refuses to start on a fault.", risk="caution"),
    _sw("FUNC_CT_DIRECTION_REVERSED", 179, 0, "CT clamp reversed", "functions",
        "Flips the reading of the grid current clamp, for a clamp installed the wrong way round.", risk="caution"),
    Setting("HOLD_CT_POWER_OFFSET", 119, "CT power offset", "functions",
            "A correction added to the current clamp's reading.", unit="W", signed=True, lo=-1000, hi=1000, risk="caution"),
    _sw("FUNC_SPORADIC_CHARGE", 233, 12, "Sporadic charge", "functions", "EG4's “Sporadic charge” switch." + UNDOC),
    _sw("FUNC_BATTERY_BACKUP_CTRL", 233, 1, "Battery backup control", "functions",
        "EG4's battery backup switch (separate from the backup output)." + UNDOC),

    # ------------------------------------------------------------------ grid connection and protection
    Setting("HOLD_CONNECT_TIME", 23, "Wait before connecting to the grid", "grid",
            "After power-up, the grid must be steady for this long before the inverter connects to it.",
            unit="s", lo=30, hi=600, risk="danger"),
    Setting("HOLD_RECONNECT_TIME", 24, "Wait before reconnecting after a grid fault", "grid",
            "After the grid drops out or goes out of range, it must be steady this long before the inverter uses it again.",
            unit="s", lo=0, hi=900, risk="danger"),
    Setting("HOLD_GRID_VOLT_CONN_LOW", 25, "Lowest grid voltage to connect", "grid",
            "The grid is only accepted above this voltage.", lo=0, hi=300, risk="danger", **V),
    Setting("HOLD_GRID_VOLT_CONN_HIGH", 26, "Highest grid voltage to connect", "grid",
            "The grid is only accepted below this voltage.", lo=0, hi=300, risk="danger", **V),
    Setting("HOLD_GRID_FREQ_CONN_LOW", 27, "Lowest grid frequency to connect", "grid",
            "The grid is only accepted above this frequency.", unit="Hz", div=100, lo=45, hi=65, risk="danger"),
    Setting("HOLD_GRID_FREQ_CONN_HIGH", 28, "Highest grid frequency to connect", "grid",
            "The grid is only accepted below this frequency.", unit="Hz", div=100, lo=45, hi=65, risk="danger"),
    *[Setting(f"HOLD_GRID_VOLT_LIMIT{n}_{side}", 29 + 4 * (n - 1) + i, f"Grid voltage trip {n}: {side.lower()}", "grid",
              f"Disconnect from the grid when its voltage stays {'below' if side == 'LOW' else 'above'} this for the "
              f"trip time. Stage {n} of 3; later stages are wider and trip faster.", lo=0, hi=300, risk="danger", **V)
      for n in (1, 2, 3) for i, side in ((0, "LOW"), (1, "HIGH"))],
    *[Setting(f"HOLD_GRID_VOLT_LIMIT{n}_{side}_TIME", 31 + 4 * (n - 1) + i, f"Grid voltage trip {n}: {side.lower()} time", "grid",
              f"How long the voltage must stay out of range before stage {n} trips, in the inverter's units.",
              lo=0, hi=65535, risk="danger")
      for n in (1, 2, 3) for i, side in ((0, "LOW"), (1, "HIGH"))],
    *[Setting(f"HOLD_GRID_FREQ_LIMIT1_{side}", 42 + i, f"Grid frequency trip: {side.lower()}", "grid",
              f"Disconnect from the grid when its frequency stays {'below' if side == 'LOW' else 'above'} this for the trip time.",
              unit="Hz", div=100, lo=45, hi=65, risk="danger") for i, side in ((0, "LOW"), (1, "HIGH"))],
    *[Setting(f"HOLD_GRID_FREQ_LIMIT1_{side}_TIME", 44 + i, f"Grid frequency trip: {side.lower()} time", "grid",
              "How long the frequency must stay out of range before it trips, in the inverter's units.", lo=0, hi=65535,
              risk="danger") for i, side in ((0, "LOW"), (1, "HIGH"))],
    _sw("FUNC_ANTI_ISLAND_EN", 21, 4, "Anti-islanding", "grid",
        "Stops the inverter feeding a dead grid, which protects line workers. Required wherever power can flow back.", risk="danger"),
    _sw("FUNC_NEUTRAL_DETECT_EN", 21, 5, "Neutral detection", "grid", "Checks the grid neutral is connected before using the grid.", risk="danger"),
    _sw("FUNC_GFCI_EN", 21, 13, "Ground-fault protection (GFCI)", "grid", "Trips on current leaking to ground.", risk="danger"),
    _sw("FUNC_DCI_EN", 21, 14, "DC injection protection", "grid", "Trips if DC leaks into the AC side.", risk="danger"),
    _sw("FUNC_LVRT_EN", 21, 3, "Low-voltage ride-through", "grid", "Stays connected through brief grid voltage dips.", risk="danger"),
    _sw("FUNC_GRID_ON_POWER_SS_EN", 21, 6, "Grid soft start", "grid", "Ramps power up slowly after connecting to the grid.", risk="danger"),
    _sw("FUNC_OVF_LOAD_DERATE_EN", 21, 1, "Over-frequency derating", "grid", "Reduces power when the grid frequency runs high.", risk="danger"),
    _sw("FUNC_DRMS_EN", 21, 2, "Demand response (DRMS)", "grid", "Australian demand-response input. Not used in the US.", risk="danger"),

    # ------------------------------------------------------------------ about this inverter (read-only)
    Setting("HOLD_DEVICE_TYPE_CODE", 19, "Device type code", "info", "EG4's code for this model family (54 or 38 for the 6000XP).", writable=False),
    Setting("HOLD_PV_INPUT_MODE", 20, "Solar input mode", "info", "How the solar inputs are wired, set at installation.", writable=False),
    Setting("HOLD_EPS_VOLT_SET", 90, "Output voltage", "info", "The inverter's AC output voltage.", unit="V", writable=False),
    Setting("HOLD_EPS_FREQ_SET", 91, "Output frequency", "info", "The inverter's AC output frequency.", unit="Hz", writable=False),
    Setting("HOLD_NOMINAL_BATTERY_VOLTAGE", 148, "Battery nominal voltage", "info", "The battery bank's rated voltage.", writable=False, **V),
    Setting("HOLD_SET_MASTER_OR_SLAVE", 112, "Parallel role", "info", "0 for a single inverter.", writable=False),
    Setting("HOLD_SET_COMPOSED_PHASE", 113, "System type", "info", "Phase setup for single or parallel inverters.", writable=False),
]
BY_KEY = {s.key: s for s in SETTINGS}
assert len(BY_KEY) == len(SETTINGS), "setting keys must be unique"

# Shown in demo mode (a 6000XP with a 280 Ah lithium bank, its own BMS, grid charging off).
DEMO = dict(FUNC_AC_CHARGE=True, BIT_AC_CHARGE_TYPE=0, HOLD_AC_CHARGE_WINDOW_1="01:00-05:00", HOLD_AC_CHARGE_POWER_CMD=6.0,
            HOLD_AC_CHARGE_SOC_LIMIT=101, HOLD_AC_CHARGE_START_BATTERY_SOC=20, HOLD_AC_CHARGE_END_BATTERY_SOC=100,
            HOLD_AC_CHARGE_START_BATTERY_VOLTAGE=46.4, HOLD_AC_CHARGE_END_BATTERY_VOLTAGE=54.0, HOLD_AC_CHARGE_BATTERY_CURRENT=60,
            HOLD_LEAD_ACID_CHARGE_VOLT_REF=56.0, HOLD_FLOATING_VOLTAGE=54.0, HOLD_LEAD_ACID_CHARGE_RATE=125,
            HOLD_CHARGE_POWER_PERCENT_CMD=100, HOLD_SYSTEM_CHARGE_SOC_LIMIT=101, HOLD_SYSTEM_CHARGE_VOLT_LIMIT=57.6,
            HOLD_EQUALIZATION_VOLTAGE=58.8, HOLD_EQUALIZATION_PERIOD=30, HOLD_LEAD_ACID_TEMPR_LOWER_LIMIT_CHG=0,
            HOLD_LEAD_ACID_TEMPR_UPPER_LIMIT_CHG=45, HOLD_SOC_LOW_LIMIT_EPS_DISCHG=15, HOLD_LEAD_ACID_DISCHARGE_CUT_OFF_VOLT=44.0,
            HOLD_DISCHG_CUT_OFF_SOC_EOD=20, HOLD_ON_GRID_EOD_VOLTAGE=46.0, BIT_ON_GRID_EOD_TYPE=1, BIT_DISCHG_CONTROL_TYPE=1,
            HOLD_BATTERY_LOW_TO_UTILITY_SOC=20, HOLD_BATTERY_LOW_TO_UTILITY_VOLTAGE=46.0, HOLD_BATTERY_WARNING_SOC=20,
            HOLD_BATTERY_WARNING_RECOVERY_SOC=30, HOLD_BATTERY_WARNING_VOLTAGE=44.0, HOLD_BATTERY_WARNING_RECOVERY_VOLTAGE=47.0,
            HOLD_LEAD_ACID_DISCHARGE_RATE=125, HOLD_DISCHG_POWER_PERCENT_CMD=100, HOLD_LEAD_ACID_TEMPR_LOWER_LIMIT_DISCHG=-20,
            HOLD_LEAD_ACID_TEMPR_UPPER_LIMIT_DISCHG=55, HOLD_OUTPUT_CONFIGURATION=0, HOLD_LINE_MODE_INPUT=0,
            HOLD_MAX_AC_INPUT_POWER=9600, FUNC_SET_TO_STANDBY=True, FUNC_EPS_EN=True, FUNC_SW_SEAMLESSLY_EN=True,
            FUNC_BUZZER_EN=True, BIT_GENERATOR_CHARGE_TYPE=1, OFF_GRID_HOLD_GEN_CHG_START_SOC=20, OFF_GRID_HOLD_GEN_CHG_END_SOC=100,
            OFF_GRID_HOLD_GEN_CHG_START_VOLT=46.4, OFF_GRID_HOLD_GEN_CHG_END_VOLT=54.0, OFF_GRID_HOLD_MAX_GEN_CHG_BAT_CURR=40,
            HOLD_MAX_GENERATOR_INPUT_POWER=5000, FUNC_GEN_PEAK_SHAVING=True, _12K_HOLD_SMART_LOAD_START_SOC=90,
            _12K_HOLD_SMART_LOAD_END_SOC=60, _12K_HOLD_SMART_LOAD_START_VOLT=54.0, _12K_HOLD_SMART_LOAD_END_VOLT=48.0,
            _12K_HOLD_AC_COUPLE_START_SOC=50, _12K_HOLD_AC_COUPLE_END_SOC=90, _12K_HOLD_AC_COUPLE_START_VOLT=50.0,
            _12K_HOLD_AC_COUPLE_END_VOLT=54.0, FUNC_PV_ARC=True, FUNC_RSD_DISABLE=False, FUNC_ISO_EN=True,
            HOLD_CONNECT_TIME=60, HOLD_RECONNECT_TIME=300, HOLD_GRID_VOLT_CONN_LOW=211.2, HOLD_GRID_VOLT_CONN_HIGH=264.0,
            HOLD_GRID_FREQ_CONN_LOW=59.5, HOLD_GRID_FREQ_CONN_HIGH=60.1, HOLD_GRID_VOLT_LIMIT1_LOW=211.2,
            HOLD_GRID_VOLT_LIMIT1_HIGH=264.0, HOLD_GRID_VOLT_LIMIT2_LOW=120.0, HOLD_GRID_VOLT_LIMIT2_HIGH=288.0,
            HOLD_GRID_FREQ_LIMIT1_LOW=59.5, HOLD_GRID_FREQ_LIMIT1_HIGH=60.5, FUNC_ANTI_ISLAND_EN=True, FUNC_NEUTRAL_DETECT_EN=True,
            HOLD_DEVICE_TYPE_CODE=54, HOLD_PV_INPUT_MODE=4, HOLD_EPS_VOLT_SET=240, HOLD_EPS_FREQ_SET=60,
            HOLD_NOMINAL_BATTERY_VOLTAGE=48.0, HOLD_SET_COMPOSED_PHASE=1)


# --------------------------------------------------------------------------- registers
def blocks(max_count=40):
    """(start, count) reads covering every register the settings use."""
    regs = sorted({r for s in SETTINGS for r in s.regs})
    out = []
    for r in regs:
        if out and r < out[-1][0] + max_count:
            out[-1] = (out[-1][0], r - out[-1][0] + 1)
        else:
            out.append((r, 1))
    return out


def writable_registers():
    return {r for s in SETTINGS if s.writable for r in s.regs}


def _mask(s):
    return ((1 << s.width) - 1) << s.bit


def _hhmm(v):
    return f"{v & 0xFF:02d}:{v >> 8:02d}"


def decode(s, regs):
    """The setting's value from raw holding registers: a number, bool, option number or "HH:MM-HH:MM"; None if not read."""
    if any(r not in regs for r in s.regs):
        return None
    raw = regs[s.reg]
    if s.kind == "window":
        return f"{_hhmm(raw)}-{_hhmm(regs[s.reg + 1])}"
    if s.bit is not None:
        v = (raw & _mask(s)) >> s.bit
        return bool(v) if s.kind == "switch" else v
    if s.signed and raw >= 0x8000:
        raw -= 0x10000
    return raw / s.div if s.div != 1 else raw


WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def encode(s, value, regs):
    """{register: new raw value} that sets `value`, keeping the other bits of a shared register (taken from `regs`).
    Raises ValueError with a message fit for the page."""
    if not s.writable:
        raise ValueError(f"{s.label} can't be changed here")
    if s.kind == "window":
        m = WINDOW.match(str(value))
        if not m:
            raise ValueError(f"{s.label}: use a time range like 01:00-05:00 (00:00-00:00 for off)")
        h1, m1, h2, m2 = map(int, m.groups())
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
            raise ValueError(f"{s.label}: hours go 0-23 and minutes 0-59")
        return {s.reg: h1 | m1 << 8, s.reg + 1: h2 | m2 << 8}
    if s.bit is not None:
        if s.reg not in regs:
            raise ValueError(f"{s.label}: the current value hasn't been read yet")
        if s.kind == "switch":
            if not isinstance(value, bool):
                raise ValueError(f"{s.label}: expected on or off")
            v = int(value)
        else:
            try:
                v = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{s.label}: pick one of the choices")
            if v not in s.options:
                raise ValueError(f"{s.label}: pick one of the choices")
        return {s.reg: (regs[s.reg] & ~_mask(s) & 0xFFFF) | (v << s.bit)}
    if s.kind == "select":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{s.label}: pick one of the choices")
        if v not in s.options:
            raise ValueError(f"{s.label}: pick one of the choices")
        return {s.reg: v}
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{s.label}: enter a number")
    if (s.lo is not None and num < s.lo) or (s.hi is not None and num > s.hi):
        raise ValueError(f"{s.label} must be between {fmt(s, s.lo)} and {fmt(s, s.hi)}")
    raw = round(num * s.div)
    if abs(raw / s.div - num) > 1e-6:
        raise ValueError(f"{s.label}: steps of {fmt(s, s.step)}")
    if s.signed:
        if not -0x8000 <= raw < 0x8000:
            raise ValueError(f"{s.label}: out of range")
        raw &= 0xFFFF
    elif not 0 <= raw <= 0xFFFF:
        raise ValueError(f"{s.label}: out of range")
    return {s.reg: raw}


def fmt(s, v):
    if v is None:
        return "-"
    if s.kind == "number":
        txt = f"{v:.{len(str(s.div)) - 1}f}" if s.div != 1 else f"{v:g}"
        return f"{txt} {s.unit}".strip()
    if s.kind == "switch":
        return "On" if v else "Off"
    if s.kind == "select":
        return s.options.get(v, f"Unknown ({v})")
    return "Off" if v == "00:00-00:00" else v.replace("-", " to ")


def demo_registers():
    regs = {r: 0 for s in SETTINGS for r in s.regs}
    for k, v in DEMO.items():
        s = BY_KEY[k]
        regs.update(encode(Setting(**{**s.__dict__, "writable": True, "lo": None, "hi": None}), v, regs))
    return regs


# --------------------------------------------------------------------------- the check against EG4's own decoding
def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def cloud_value(s, params):
    """The setting's value as EG4 Monitor reports it (remoteRead), in the same terms as decode(); None if missing."""
    keys = s.cloud_keys
    if any(k not in params or params[k] in (None, "") for k in keys):
        return None
    try:
        if s.kind == "window":
            h1, m1, h2, m2 = (int(float(params[k])) for k in keys)
            return f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"
        v = params[keys[0]]
        if s.kind == "switch":
            return _truthy(v)
        if s.kind == "select":
            return int(float(v))
        return float(v)
    except (TypeError, ValueError):
        return None


def same(s, a, b):
    if a is None or b is None:
        return False
    if s.kind == "number":
        return abs(float(a) - float(b)) <= s.step / 2 + 1e-9
    return a == b


def weak(s, v):
    """A value that matches whichever register it's read from (0, off, no window): the match alone proves little."""
    return v in (0, 0.0, False, "00:00-00:00", None)


def compare(local_regs, params, confirmed=()):
    """Each setting's check result, comparing this map's decoding of the raw registers with EG4's decoding (remoteRead):
       match       same value, and either a telling one or confirmed by a one-register read (`confirmed` keys) -> writable
       weak        same value, but 0/off on both sides and not confirmed yet
       raw         EG4 reports the raw register number: the place is right but the unit/scale here is wrong -> stays locked
       mismatch    different values -> stays locked
       missing     EG4 doesn't report this setting for this inverter -> stays locked
    Returns {key: {status, local, cloud}}."""
    out = {}
    for s in SETTINGS:
        loc, cl = decode(s, local_regs), cloud_value(s, params)
        if cl is None:
            st = "missing"
        elif same(s, loc, cl):
            st = "weak" if weak(s, loc) and s.key not in confirmed else "match"
        elif s.kind == "number" and s.div != 1 and loc is not None and abs(local_regs[s.reg] - cl) < 0.5:
            st = "raw"
        else:
            st = "mismatch"
        out[s.key] = dict(status=st, local=loc, cloud=cl)
    return out
