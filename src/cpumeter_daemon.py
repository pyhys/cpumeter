#!/usr/bin/env python3

"""Tools to run a Desynn instrument as a CPU meter"""

import asyncio
import enum
import math
import pathlib
import statistics
import time
from typing import Tuple, Optional, List

import psutil  # type: ignore
import pyftdi  # type: ignore
from pyftdi.spi import SpiController, SpiGpioPort, SpiPort  # type: ignore

#           FTDI board      DAC
# SCLK      D0              3
# MOSI      D1              4
# MISO      not used        -
# CS0       D3              2
# CS1       D4              2
# CS2       D5              2
# GPIO      D6              -
# +3V       +3V             1 Vdd
# GND       GND             7 Vss
#                           5 LDAC
#                           6 Voutb
#                           8 Vouta

# DAC chip MCP4812 2-channel 10 bit

DAC_CHANNEL_A = 0
DAC_CHANNEL_B = 1
DAC_GAIN_1 = 1
DAC_GAIN_2 = 2
DAC_MAX_INPUT = 1023
DAC_MAX_OUTPUT = 2.004  # Volt
GPIO_PIN_D6 = 6
SPI_URL = "ftdi://ftdi:232h/1"
SPI_FREQUENCY = 12e6
CPU_AVERAGING_SAMPLES = 20
CPU_SAMPLE_TIME = 0.1  # seconds
SLEEPTIME_PIN_TOGGLE = 0.3  # seconds
TIMESTAMP_AGE_SLEEP = 3600  # seconds
TIMESTAMP_AGE_DEMO = 2  # seconds
PATH_TIMESTAMP = "/tmp/cpumeter_timestamp"

# Adjust to your particular instrument
AMPLIFIER_GAIN = 11.0
VOLTAGE_OUT_MIN = 7.0  # Volt
VOLTAGE_OUT_MAX = 20.0  # Volt
ROTATION_UP_A = 0.93  # Turns
ROTATION_UP_B = 0.38  # Turns
SCALE_A_BEGIN = 0.52  # Turns
SCALE_A_END = 1.0  # Turns
SCALE_B_BEGIN = 0.47  # Turns
SCALE_B_END = -0.03  # Turns


class State(enum.Enum):
    """CPU measurement state"""

    DEMO = 1
    CPU_MEAS = 2
    SLEEP = 3


class Smoother:
    def __init__(self, number_of_elements: int) -> None:
        """Smoothen the min- and max values of measurements.

        The min values and max values are smoothened separately.

        Args:
            number_of_elements: Number of elements to average over
        """
        self._minlist: List[float] = []
        self._maxlist: List[float] = []
        self._number_of_elements: int = number_of_elements

    def _update(self, listinstance: List[float], value: float) -> None:
        """Update a list instance with a new value.

        Args:
            listinstance:   List instance to be updated
            value:          New value
        """
        listinstance.append(value)
        if len(listinstance) > self._number_of_elements:
            listinstance.pop(0)

    def update(self, values: List[float]) -> None:
        """
        Update with new values. The min and max of the values are stored.

        Args:
            values: List of floats
        """
        self._update(self._minlist, min(values))
        self._update(self._maxlist, max(values))

    def get_min_and_max(self) -> Tuple[float, float]:
        """
        Return the smoothed min and max values respectively in a tuple
        """
        return statistics.mean(self._minlist), statistics.mean(self._maxlist)


class CpuMonitor:
    def __init__(self, running_average_samples: int, sample_time_s: float) -> None:
        """
        Monitor CPU usage

        Args:
            running_average_samples: Number of samples for averaging
            sample_time_s:           Sample time in seconds
        """
        self._smoother = Smoother(running_average_samples)
        self._sample_time_s = sample_time_s

    def get_usage(self) -> Tuple[float, float]:
        """
        Get filtered CPU usage value.

        Blocks for the sample time set in the constructor.

        Returns the min and max value of the CPU core utilisation.
        as the tuple (minvalue, maxvalue) where each value is 0.0-1.0
        """
        self._smoother.update(
            [
                x / 100
                for x in psutil.cpu_percent(interval=self._sample_time_s, percpu=True)
            ]
        )
        return self._smoother.get_min_and_max()


class Calculator:
    """Voltage calculator for one indicator.

    Symbols:
        r       Rotation in full turns
        p       Voltage position in range 0-1
        m       Scaled position 0-1, mLow to mHigh
        d       Digital value 0-dmax, where dmax typically is 1023
        Vi      Intermediate voltage, out from DAC. 0-Vimax
        Vout    Output voltage Vi multiplied by gain
    """

    def __init__(
        self,
        roffset: float,
        dmax: float,
        vimax: float,
        gain: float,
        vout_low: float,
        vout_high: float,
    ) -> None:
        """
        Initialise a calculator for voltages for one indicator.

        Args:
            roffset:    Rotional offset for needle to point upwards
            dmax:       Digital value for max voltage from DAC
            vimax:      Maximum output voltage from DAC
            gain:       Voltage gain
            vout_low:   Low limit of output voltage (Volt)
            vout_high:  High limit of output voltage (Volt)

        """
        self._roffset = roffset
        self._dmax = dmax
        self._vimax = vimax
        self._gain = gain
        self._vout_low = vout_low
        self._vout_high = vout_high

        if self._vout_high > self._vimax * self._gain:
            raise ValueError("Invalid vout_high")
        if self._vout_low > self._vimax * self._gain:
            raise ValueError("Invalid vout_low")
        self._m_high = self._vout_high / (self._vimax * self._gain)
        self._m_low = self._vout_low / (self._vimax * self._gain)

    def __str__(self) -> str:
        return "Calculator. mLow {:.2f} mHigh {:.2f}".format(self._m_low, self._m_high)

    def _calc_position(self, rotation: float) -> float:
        """Calculate the voltage position

        Args:
            rotation: Indicator rotation in turns.
                      A value 0 is straight up.
                      The indicator rotates clockwise, and is straight up
                      again at a value of 1.

        Return the voltage position, in the range 0-1
        """
        return 0.5 * (1 + math.cos((rotation - self._roffset) * 2 * math.pi))

    def _calc_rel_position(self, position: float) -> float:
        """Calculate the limited relative (voltage) position

        Args:
            rotation: Indicator rotation in turns.
                      A value 0 is straight up.
                      The indicator rotates clockwise, and is straight up
                      again at a value of 1.

        Return the relative voltage position (range smaller than 0-1)
        """
        return position * (self._m_high - self._m_low) + self._m_low

    def _calc_digital_value(self, rel_position: float) -> int:
        """Calculate the digital value for sending to the DAC

        Args:
            rel_position: Relative voltage, range smaller than 0-1

        Returns the digital value for sending to the DAC."""
        return int(rel_position * self._dmax)

    def _calc_intermediate_voltage(self, digital_value: int) -> float:
        """Calculate the output voltage from the DAC.

        Args:
            digital_value: Digital value to the DAC

        Returns the output voltage from the DAC, in Volt."""

        return digital_value * self._vimax / self._dmax

    def _calc_output_voltage(self, intermediate_voltage: float) -> float:
        """Calculate the amplifier output voltage.

        Args:
            intermediate_voltage: Intermediate voltage from DAC, in Volt

        Return the output voltage in Volt
        """
        return intermediate_voltage * self._gain

    def get_positions(self, rotation: float) -> Tuple[float, float, float]:
        """Get the voltage positions for a given rotation.

        Args:
            rotation: Indicator rotation in turns.
                      A value 0 is straight up.
                      The indicator rotates clockwise, and is straight up
                      again at a value of 1.

        Returns the three positions in range 0-1.
        """
        return (
            self._calc_position(rotation),
            self._calc_position(rotation + 1 / 3),
            self._calc_position(rotation + 2 / 3),
        )

    def get_rel_positions(self, rotation: float) -> Tuple[float, float, float]:
        """Get the relative (voltage) position for a given rotation.

        Args:
            rotation: Indicator rotation in turns.
                      A value 0 is straight up.
                      The indicator rotates clockwise, and is straight up
                      again at a value of 1.

        Returns the three relative positions (range smaller than 0-1)
        """
        pa, pb, pc = self.get_positions(rotation)
        return (
            self._calc_rel_position(pa),
            self._calc_rel_position(pb),
            self._calc_rel_position(pc),
        )

    def get_digital_values(self, rotation: float) -> Tuple[int, int, int]:
        """Get the digital values for a given rotation.

        Args:
            rotation: Indicator rotation in turns.
                      A value 0 is straight up.
                      The indicator rotates clockwise, and is straight up
                      again at a value of 1.

        Returns the three digital values for sending to the DACs.
        """
        ma, mb, mc = self.get_rel_positions(rotation)
        return (
            self._calc_digital_value(ma),
            self._calc_digital_value(mb),
            self._calc_digital_value(mc),
        )

    def get_intermediate_voltages(self, rotation: float) -> Tuple[float, float, float]:
        """Get the intermediate voltages (DAC output voltages) for a given rotation.

        Args:
            rotation: Indicator rotation in turns.
                      A value 0 is straight up.
                      The indicator rotates clockwise, and is straight up
                      again at a value of 1.

        Returns the three intermediate voltages in Volt.
        """
        da, db, dc = self.get_digital_values(rotation)
        return (
            self._calc_intermediate_voltage(da),
            self._calc_intermediate_voltage(db),
            self._calc_intermediate_voltage(dc),
        )

    def get_output_voltages(self, rotation: float) -> Tuple[float, float, float]:
        """Get the amplifier output voltages for a given rotation.

        Args:
            rotation: Indicator rotation in turns.
                      A value 0 is straight up.
                      The indicator rotates clockwise, and is straight up
                      again at a value of 1.

        Returns the three amplifier output voltages in Volt.
        """
        via, vib, vic = self.get_intermediate_voltages(rotation)
        return (
            self._calc_output_voltage(via),
            self._calc_output_voltage(vib),
            self._calc_output_voltage(vic),
        )


def get_file_timestamp_age(timestamp_path: pathlib.Path) -> Optional[float]:
    """Get the timestamp age in seconds.

    Reads the modification time of the timestamp file.

    Args:
        timestamp_path: Path to timestamp file

    Returns the timestamp age in seconds, or None if not found.
    """
    if not timestamp_path.exists():
        return None

    return time.time() - timestamp_path.stat().st_mtime


class DigOutputPin:
    """Digital output pin"""

    def __init__(self, gpio: SpiGpioPort, pinnumber: int) -> None:
        """Initialise a digital output pin

        Args:
            gpio:       GPIO instance
            pinnumber:  Pin number
        """
        self.gpio = gpio
        self.pinnumber = pinnumber
        self._set_direction_out()

    @property
    def pinmask(self) -> int:
        """Calculate the pin mask for the pin"""
        return 1 << self.pinnumber

    def _calculate_new_combined_value(
        self, new_state: bool, old_combined_value: int
    ) -> int:
        """Calculate the combined value for an entire GPIO bank.

        Args:
            new_state:          New state of digital pin
            old_combined_value: Combined value of GPIO bank
        """
        new_combined_value = old_combined_value
        if new_state:
            new_combined_value |= self.pinmask
        else:
            new_combined_value &= ~self.pinmask

        return new_combined_value

    def _extract_pin_state(self, combined_value: int) -> bool:
        """Extract the pin state

        Returns True when the pin is on.
        """
        return (combined_value & self.pinmask) > 0

    def _set_direction_out(self) -> None:
        """Set the direction to out for the digital pin"""
        self.gpio.set_direction(self.pinmask, self.pinmask)

    def set_output(self, value: bool) -> None:
        """
        Set output value of digital pin.

        Args:
            value: True to set the pin high.
        """
        self.gpio.write(
            self._calculate_new_combined_value(value, self.gpio.read(with_output=True))
        )

    def get_output(self) -> bool:
        """
        Get the value of digital pin.

        Return True for high state.
        """
        return self._extract_pin_state(self.gpio.read(with_output=True))

    def toggle(self) -> None:
        """
        Toggle the state of the pin.
        """
        self.set_output(not self.get_output())


class DacChannel:
    """Single DAC channel representation.

    The DAC chip contains two DAC channels.
    """

    def __init__(
        self,
        dac_chip: SpiPort,
        channel: int,
        gain: int = DAC_GAIN_1,
        enabled: bool = True,
    ) -> None:
        """Initialise a DAC channel representation.

        Args:
            dac_chip:   DAC chip SPI port
            channel:    DAC_CHANNEL_A or DAC_CHANNEL_B
            gain:       DAC_GAIN_1 or DAC_GAIN_2 (x1 or x2)
            enabled:    True if the channel is enabled
        """
        self.dac_chip = dac_chip
        self.channel = channel
        self.gain = gain
        self.enabled = enabled

    def _calculate_combined_value(self, value: int) -> Tuple[int, int]:
        """Calculate value to write to DAC.

        Includes the output value, channel number, gain and enabled.

        Args:
            value:  Digital value for converting to analog. Allowed values 0-1023

        Returns two bytes as an tuple of ints.
        """
        combined_value = (int(abs(value)) & 0x03FF) << 2
        if self.channel == DAC_CHANNEL_B:
            combined_value |= 1 << 15
        if self.gain == DAC_GAIN_1:
            combined_value |= 1 << 13
        if self.enabled:
            combined_value |= 1 << 12

        return (combined_value >> 8), (combined_value & 0xFF)

    def set_output(self, value: int) -> None:
        """
        Set analog output value.

        This takes approx 50 us.

        Args:
            value:  Digital value for converting to analog. Allowed values 0-1023
        """
        self.dac_chip.write(self._calculate_combined_value(value))


class Indicator:
    """Indicator, not considering the printed scale on the front."""

    def __init__(
        self,
        roffset: float,
        dmax: float,
        vimax: float,
        gain: float,
        vout_low: float,
        vout_high: float,
        dac_0: DacChannel,
        dac_1: DacChannel,
        dac_2: DacChannel,
    ) -> None:
        """Initialise the indicator.

        Args:
            roffset:    Rotional offset for needle to point upwards
            dmax:       Digital value for max voltage from DAC
            vimax:      Maximum output voltage from DAC
            gain:       Voltage gain
            vout_low:   Low limit of output voltage (Volt)
            vout_high:  High limit of output voltage (Volt)
            dac_0:      DAC 0. Arrange them so needle turns clockwise for larger values
            dac_1:      DAC 1
            dac_2:      DAC 2
        """
        self._calculator = Calculator(roffset, dmax, vimax, gain, vout_low, vout_high)
        self._dac_0 = dac_0
        self._dac_1 = dac_1
        self._dac_2 = dac_2

    def set_output(self, rotation: float) -> None:
        """
        Set the rotation of the indicator.

        Args:
            rotation: Indicator rotation in turns.
                      A value 0 is straight up.
                      The indicator rotates clockwise, and is straight up
                      again at a value of 1.
        """
        d1, d2, d3 = self._calculator.get_digital_values(rotation)
        self._dac_0.set_output(d1)
        self._dac_1.set_output(d2)
        self._dac_2.set_output(d3)


class Scale:
    def __init__(self, indic: Indicator, rot0: float, rot1: float) -> None:
        """
        A scale drawn on an indicator.

        Args:
            indic: Indiator instance
            rot0: Rotation value for needle to reach min
            rot1: Rotation value for needle to reach max

        For clockwise rotation the rot1 value should be larger than rot0.
        """
        self._indic = indic
        self._rot0 = rot0
        self._rot1 = rot1
        self._clockwise: bool = rot1 > rot0
        self._factor: float = rot1 - rot0
        self._current_rotation: float = 0  # Relative to up
        self.set_scale(0)

    def _calc_rotation(self, value: float) -> float:
        """Calculate the rotation of the indicator

        Args:
            value: Use 0 for beginning of scale, and 1 for end of scale.

        Returns the rotation value.
        """
        value = min(max(0, value), 1)
        return self._rot0 + value * self._factor

    def get_rotation(self) -> float:
        """Get the current rotation"""
        return self._current_rotation

    def set_scale(self, value: float) -> None:
        """Set the scale value

        Args:
            value: Use 0 for beginning of scale, and 1 for end of scale.
        """
        self._current_rotation = self._calc_rotation(value)
        self._indic.set_output(self._current_rotation)

    def set_turns(self, turns: float) -> None:
        """Set number of turns relative to beginning of scale.

        Args:
            turns: Number of turns. Must be >= 0.
        """
        if turns < 0:
            return
        if self._clockwise:
            self._current_rotation = self._rot0 + turns
        else:
            self._current_rotation = self._rot0 - turns
        self._indic.set_output(self._current_rotation)


class DemoRunner:
    def __init__(self, sc: Scale) -> None:
        """Demo runner

        Args:
            sc:     Scale instance
        """
        self.sc = sc
        self._step_size = 0.02
        self._step_time = 0.01
        self._allowed_error = self._step_size * 0.7

    async def _rotate_turns_smooth(self, start_turns: float, end_turns: float) -> None:
        """Rotate a number of turns smoothly

        Relative to scale start position.

        Args:
            start_turns:    Initial number of turns
            end_turns:      Final number of turns
        """
        is_increasing = end_turns > start_turns
        current_turns = start_turns
        while True:
            error = end_turns - current_turns
            if abs(error) < self._allowed_error:
                break
            if is_increasing:
                current_turns += self._step_size
            else:
                current_turns -= self._step_size
            self.sc.set_turns(current_turns)
            await asyncio.sleep(self._step_time)

    async def _set_scale_smooth(self, start_value: float, end_value: float) -> None:
        """Rotate to a scale position smoothly

        Args:
            start_value:    Initial position
            end_value:      Final position
        """
        is_increasing = end_value > start_value
        current_value = start_value
        while True:
            error = end_value - current_value
            if abs(error) < self._allowed_error:
                break
            if is_increasing:
                current_value += self._step_size
            else:
                current_value -= self._step_size
            self.sc.set_scale(current_value)
            await asyncio.sleep(self._step_time)

    async def run(self) -> None:
        """Run the demo"""
        self.sc.set_scale(0)
        await asyncio.sleep(0.2)
        await self._rotate_turns_smooth(0, 1)
        await self._set_scale_smooth(0, 1)
        await asyncio.sleep(0.1)
        await self._set_scale_smooth(1, 0)


class Toggler:
    def __init__(self, pin: DigOutputPin, sleeptime: float) -> None:
        """
        Continously toggle a digital output pin.

        Args:
            pin:       Pin instance to toggle
            sleeptime: Sleep time in seconds
        """
        self._pin = pin
        self._sleeptime = sleeptime

    async def run(self) -> None:
        """Run toggling of the pin"""
        while True:
            self._pin.toggle()
            await asyncio.sleep(self._sleeptime)


def set_up_hardware() -> Tuple[Scale, Scale, DigOutputPin]:
    """
    Set up the hardware.

    Returns the two scale instances and an digital output pin instance.
    """
    spi = SpiController(cs_count=3)
    spi.configure(SPI_URL, frequency=SPI_FREQUENCY)

    gpio = spi.get_gpio()
    digpin = DigOutputPin(gpio, GPIO_PIN_D6)

    dac_chip0 = spi.get_port(cs=0, mode=0)
    dac_chip1 = spi.get_port(cs=1, mode=0)
    dac_chip2 = spi.get_port(cs=2, mode=0)

    dac_a1 = DacChannel(dac_chip0, DAC_CHANNEL_B)  # J2p1
    dac_a2 = DacChannel(dac_chip1, DAC_CHANNEL_B)  # J2p2
    dac_a3 = DacChannel(dac_chip0, DAC_CHANNEL_A)  # J2p3
    dac_b1 = DacChannel(dac_chip2, DAC_CHANNEL_B)  # J2p4
    dac_b2 = DacChannel(dac_chip1, DAC_CHANNEL_A)  # J2p5
    dac_b3 = DacChannel(dac_chip2, DAC_CHANNEL_A)  # J2p6

    indicator_a = Indicator(
        ROTATION_UP_A,
        DAC_MAX_INPUT,
        DAC_MAX_OUTPUT,
        AMPLIFIER_GAIN,
        VOLTAGE_OUT_MIN,
        VOLTAGE_OUT_MAX,
        dac_a2,  # Order to have correct rotation direction
        dac_a1,
        dac_a3,
    )
    indicator_b = Indicator(
        ROTATION_UP_B,
        DAC_MAX_INPUT,
        DAC_MAX_OUTPUT,
        AMPLIFIER_GAIN,
        VOLTAGE_OUT_MIN,
        VOLTAGE_OUT_MAX,
        dac_b1,
        dac_b2,
        dac_b3,
    )

    scale_a = Scale(indicator_a, SCALE_A_BEGIN, SCALE_A_END)
    scale_b = Scale(indicator_b, SCALE_B_BEGIN, SCALE_B_END)

    return scale_a, scale_b, digpin


async def main() -> None:
    """Main CPUmeter application"""
    scale_a, scale_b, digpin = set_up_hardware()
    path_timestamp = pathlib.Path(PATH_TIMESTAMP)
    monitor = CpuMonitor(CPU_AVERAGING_SAMPLES, CPU_SAMPLE_TIME)
    demo_a = DemoRunner(scale_a)
    demo_b = DemoRunner(scale_b)
    pintoggler = Toggler(digpin, SLEEPTIME_PIN_TOGGLE)

    state = State.SLEEP
    print("Starting CPU meter application", flush=True)
    while True:
        age = get_file_timestamp_age(path_timestamp)
        if age is None:
            age = 2 * TIMESTAMP_AGE_SLEEP

        if state != State.DEMO and age < TIMESTAMP_AGE_DEMO:
            print("Starting demo", flush=True)
            state = State.DEMO
        elif state == State.CPU_MEAS and age > TIMESTAMP_AGE_SLEEP:
            print("Going to sleep", flush=True)
            state = State.SLEEP

        if state == State.CPU_MEAS:
            digpin.toggle()
            mincpu, maxcpu = monitor.get_usage()
            scale_a.set_scale(mincpu)
            scale_b.set_scale(maxcpu)
        elif state == State.DEMO:
            pin_task = asyncio.create_task(pintoggler.run())
            await asyncio.gather(demo_a.run(), demo_b.run())
            pin_task.cancel()

            state = State.CPU_MEAS
            print("Starting CPU measurement", flush=True)
        else:
            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (pyftdi.usbtools.UsbToolsError, pyftdi.ftdi.FtdiError):
        print("Error: Cpumeter could not find FTDI unit via USB", flush=True)
