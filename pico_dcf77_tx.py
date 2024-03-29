# DCF77 transmitter for Raspberry Pi Pico W
# ------------------------------------------------------
# Copyright (c) 2023, Elehobica
#
# This software is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this software.  If not, see <http://www.gnu.org/licenses/>.
#
# As for the libraries which are used in this software, they can have
# different license policies, look at the subdirectories of lib directory.
# ------------------------------------------------------

import sys
import machine
import rp2
import utime
import network
import ntptime
import array
import _thread

# write ssid and password as 'secrets' dict in secrets.py
from secrets import secrets

# CET offset
TZ_CET_OFS = 1

# DCF77 carrier frequency 77500  Hz
DCF77_CARRIER_FREQ = 77500

# System Frequency to set multiplier of DCF77 frequency * 8
# it also should be 300's multiplier to realize 15.6° phase modulation
SYSTEM_FREQ = DCF77_CARRIER_FREQ * 600 * 2

# Pin Configuration
PIN_MOD_BASE = 2   # modulation (output for PIO) base, take 2 pins

# Seconds to run until re-sync with NTP
# infinite if SEC_TO_RUN == 0
SEC_TO_RUN = 60 * 60 * 24 * 7 // 2  # half week is the maximum

def connectWifi():
  ssid = secrets['ssid']
  password = secrets['password']
  wlan = network.WLAN(network.STA_IF)
  wlan.active(True)
  wlan.connect(ssid, password)
  print('Waiting for WiFi connection...')
  for t in range(10):  # timeout 10 sec
    if wlan.isconnected():
      print('WiFi connected')
      break
    utime.sleep(1)
  else:
    print('WiFi not connected')
    return False
  return True

def disconnectWifi():
  wlan = network.WLAN(network.STA_IF)
  wlan.deinit()

# LocalTime class for NTP and RTC
class LocalTime:
  # utility to handle time tuple
  class TimeTuple:
    def __init__(self, timeTuple: tuple):
      self.year, self.month, self.mday, self.hour, self.minute, self.second, self.weekday, self.yearday = timeTuple
    def __str__(self) -> str:
      wday = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[self.weekday]
      month = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')[self.month-1]
      return f'{wday}, {month} {self.mday:02d}, {self.year:04d} {self.hour:02d}:{self.minute:02d}:{self.second:02d}'
  class TzCet:
    TZ = 1  # positive only
    @classmethod
    def isSummerTime(cls, secs: int) -> bool:
      t = LocalTime.TimeTuple(utime.localtime(secs))
      # switch CET or CEST (https://github.com/lammersch/ntp-timer/)
      HHMarch   = utime.mktime((t.year, 3 , (31 - (int(5 * t.year/4 + 4)) % 7), cls.TZ, 0, 0, 0, 0))  # Time of March change to CEST
      HHOctober = utime.mktime((t.year, 10, (31 - (int(5 * t.year/4 + 1)) % 7), cls.TZ, 0, 0, 0, 0))  # Time of October change to CET
      if secs < HHMarch:
        return False
      elif secs < HHOctober:
        return True
      else:
        return False
    @classmethod
    def localtime(cls, *args: list) -> LocalTime.TimeTuple:
      secs = utime.time() if len(args) < 1 else args[0]
      if cls.isSummerTime(secs):
        return LocalTime.localtime(secs + (cls.TZ + 1) * 3600)
      else:
        return LocalTime.localtime(secs + cls.TZ * 3600)

  @classmethod
  def syncNtp(cls) -> None:
    ntpTime = cls.__setNtpTime()
    print(f'NTP: {ntpTime}')
    rtcTime = cls.__setRtc(ntpTime)
    print(f'RTC: {rtcTime}')
  @classmethod
  def __setNtpTime(cls) -> TimeTuple:
    utime.sleep(1)
    try:
      ntptime.settime()
    except OSError as e:
      if e.args[0] == 110:
        # reset when OSError: [Errno 110] ETIMEDOUT
        print(e)
        sys.exit(1)
    return LocalTime.TzCet.localtime()
  @classmethod
  def __setRtc(cls, t: TimeTuple) -> TimeTuple:
    machine.RTC().datetime((t.year, t.month, t.mday, t.weekday+1, t.hour, t.minute, t.second, 0))
    utime.sleep(1)  # wait to be reflected
    return cls.localtime()
  @classmethod
  def localtime(cls, *args: list) -> TimeTuple:
    return LocalTime.TimeTuple(utime.localtime(*args))
  @classmethod
  def alignSecondEdge(cls) -> None:
    t = cls.localtime()
    while t.second == cls.localtime().second:
      utime.sleep_ms(1)

# PIO program
@rp2.asm_pio(
  sideset_init=(rp2.PIO.OUT_LOW, rp2.PIO.OUT_LOW),
  out_shiftdir=rp2.PIO.SHIFT_RIGHT,
  autopull=True,
  pull_thresh=32,
  fifo_join=rp2.PIO.JOIN_TX,
)
def pioAsmDcf77Carrier():
  # generate 1/1200 frequency pulse against PIO clock with amplitude control by PWM
  #  sideset pin from sideset_base to output modulation pulse
  # assume ISR = 148 at program entry to make 149-times loop (corresponding to 1cyc = 1200clk / 4clk / 2turn = 150 loop)
  # use X for phase modulation by designating initial value of loop counter which determines the cycle
  # High amplitude: 100%         0°~180°: +4/4, 180°~360°: -4/4
  # Low amplitude : 12.5% by PWM 0°~180°: +1/8, 180°~360°: -1/8

  P = 0b01  # drive +
  N = 0b10  # drive -
  Z = 0b00  # drive zero

  # inst                       side    delay     # comment
  nop()                       .side(Z)           # initialize pin as 0

  # ---------------------- no side-set option to keep previous pin status (start)
  label('ClocksEnd')
  out(y, 22)                                     # get next Clocks4/16
  out(x, 1)                                      # get LowAmp (0: High Amp, 1: Low Amp)
  jmp(x_dec, 'LowAmpH1Setup')
  out(x, 1)                                      # get PhasePol (0: >= 0, 1: < 0)
  jmp(x_dec, 'HighAmpH2Setup')
  # HighAmpH1Setup
  out(x, 8)                                      # get PhaseOfs (should be corresponding value of 0 deg or +15.6 deg)
  jmp(y_dec, 'HighAmpH1_2')   .side(P)           # always jmp

  label('HighAmpH2Setup')
  out(x, 8)                                      # get PhaseOfs (should be corresponding value of -15.6 deg)
  jmp(y_dec, 'HighAmpH2_2')   .side(N)           # always jmp

  label('LowAmpH1Setup')
  out(x, 1)                                      # get PhasePol (don't use)
  out(x, 8)                                      # get PhaseOfs (should be corresponding value of 0 deg)
  jmp('LowAmpH1')
  # ---------------------- no side-set option to keep previous pin status (end)

  wrap_target()
  # Start of H1 of High Amplitude (4 * 150 clocks if no phase modulation) pulse shape: ^^^^
  label('HighAmpH1')
  jmp(y_dec, 'HighAmpH1_2')   .side(P)
  jmp('ClocksEnd')            .side(P)
  label('HighAmpH1_2')
  jmp(x_dec, 'HighAmpH1')     .side(P).delay(2)
  mov(x, isr)                 .side(P).delay(3)  # load 148
  # End of H1 of High Amplitude
  # Start of H2 of High Amplitude (4 * 150 clocks if no phase modulation) pulse shape: ____
  label('HighAmpH2')
  jmp(y_dec, 'HighAmpH2_2')   .side(N)
  jmp('ClocksEnd')            .side(N)
  label('HighAmpH2_2')
  jmp(x_dec, 'HighAmpH2')     .side(N).delay(2)
  mov(x, isr)                 .side(N).delay(3)  # load 148
  wrap()
  # End of H2 of High Amplitude

  label('LowAmpH1_6')
  jmp(x_dec, 'LowAmpH1')      .side(Z).delay(2)  # always jmp
  # Start of H1 of Low Amplitude (8 * 75 clocks only for no phase modulation) pulse shape: ^-------
  label('LowAmpH1')
  nop()                       .side(P)
  jmp(x_dec, 'LowAmpH1_6')    .side(Z).delay(3)
  mov(x, isr)                 .side(Z).delay(1)  # load 148
  jmp('LowAmpH2')             .side(Z)
  # End of H1 of Low Amplitude

  label('LowAmpH2_5')
  jmp(x_dec, 'LowAmpH2')      .side(Z).delay(3)  # always jmp
  # Start of H2 of Low Amplitude (8 * 75 clocks only for no phase modulation) pulse shape: _-------
  label('LowAmpH2')
  jmp(y_dec, 'LowAmpH2_2')    .side(N)
  jmp('ClocksEnd')            .side(Z)
  label('LowAmpH2_2')
  jmp(x_dec, 'LowAmpH2_5')    .side(Z).delay(2)
  mov(x, isr)                 .side(Z).delay(2)  # load 148
  jmp('LowAmpH1')             .side(Z)
  # End of H2 of Low Amplitude

# DCF77 class
class Dcf77:
  class TimecodeSet:
    id = 0
    @classmethod
    def create(cls) -> None:
      # explicitly generate 2 instances
      cls.timecodeSets = [Dcf77.TimecodeSet(), Dcf77.TimecodeSet()]
    @classmethod
    def getCurrent(cls) -> TimecodeSet:
      return cls.timecodeSets[cls.id]
    @classmethod
    def getNext(cls) -> TimecodeSet:
      return cls.timecodeSets[1 - cls.id]
    @classmethod
    def swap(cls) -> None:
      cls.id = 1 - cls.id
    def __init__(self):
      self.secs = None
      self.t = None
      self.vector = None
    def genTimecode(self, secs: int) -> None:
      ## Timecode generating functions ##
      def sync(**kwargs: dict) -> list[int]:
        return [2]
      def bcd(value: int, numDigits: int = 4, **kwargs: dict) -> Iterator[int]:
        for bitPos in range(0, numDigits):
          if bitPos % 4 == 0:
            bcdValue = value % 10
          yield (bcdValue >> (bitPos % 4)) & 0b1
          if bitPos % 4 == 3:
            value //= 10
      def bin(value: int, count: int = 1, **kwargs: dict) -> list[int]:
        return [value & 0b1] * count
      def parity(vector: list[int], **kwargs: dict) -> list[int]:
        return bin(sum(vector), **kwargs)
      ## Timecode ##
      # 00: M: Start of minute (0)
      # 01 ~ 14: Civil warning bits: (send 0s in this program)
      # 15: R: Call bit: abnormal transmitter operation (send 0 in this program)
      # 16: A1: Summer time annousement. Set during hour before change.
      # 17: Z1: CEST in effect
      # 18: Z2: CET in effect
      # 19: A2: Leap second announcement. Set during hour before leap second.
      # 20: S: Start of encoded time (1)
      # 21 ~ 27: Minute (00 ~ 59) BCD 1, 2, 4, 8, 10, 20, 40
      # 28: P1: Even parity of 21 ~ 28
      # 29 ~ 34: Hour (00 ~ 23) BCD 1, 2, 4, 8, 10, 20
      # 35: P2: Even parity of 29 ~ 35
      # 36 ~ 41: Day of month (01 ~ 31) BCD 1, 2, 4, 8, 10, 20
      # 42 ~ 44: Wday (1: Mon, 7: Sun) BCD 1, 2, 4
      # 45 ~ 49: Month (01 ~ 12) BCD 1, 2, 4, 8, 10
      # 50 ~ 57: Year(2) (00 ~ 99) BCD 1, 2, 4, 8, 10, 20, 40, 80
      # 58: P3: Even parity of 36 ~ 58
      # 59: Minute mark (No amplitude modulation)

      # BCD
      # 10, 20, 40, 80: 10's digit
      # 1, 2, 4, 8    : 1's digit

      self.secs = secs
      self.t = LocalTime.localtime(self.secs)
      a1 = 0
      z1 = LocalTime.TzCet.isSummerTime(self.secs)
      z2 = 1 - z1
      a2 = 0
      self.vector = []
      self.vector += bin(0, name='M') + bin(0, 14, name="CWB")  # 0 ~ 14
      self.vector += bin(0, name='R') + bin(a1) + bin(z1) + bin(z2) + bin(a2) + bin(1, name='S')  # 15 ~ 20
      self.vector += bcd(self.t.minute, 7)  # 21 ~ 27
      self.vector += parity(self.vector[21:], name='P1')  # 28
      self.vector += bcd(self.t.hour, 6)  # 29 ~ 34
      self.vector += parity(self.vector[29:], name='P2')  # 35
      self.vector += bcd(self.t.mday, 6)  # 36 ~ 41
      self.vector += bcd(self.t.weekday + 1, 3)  # 42 ~ 44
      self.vector += bcd(self.t.month, 5)  # 45 ~ 49
      self.vector += bcd(self.t.year, 8)  # 50 ~ 57
      self.vector += parity(self.vector[36:], name='P3')  # 58
      self.vector += sync(name='MM')  # 59
  def __init__(self, pinLed: machine.Pin, pinModOutBase: machine.Pin, pioAsm: Callable):
    def genFifoData(cycles: int, lowAmp: bool, phase: float = 0.0) -> int:
      CLOCKS_77500HZ_DIV8 = SYSTEM_FREQ // DCF77_CARRIER_FREQ // 8  # 150 (77500 KHz H1 term 4 cycles + H2 term 4 cycles)
      # the difference of the value of clocks between amplitudes is caused by PIO asm program
      clocks = cycles * (CLOCKS_77500HZ_DIV8 // 2) - 1 if lowAmp else cycles * (CLOCKS_77500HZ_DIV8 - 1) * 2 - 1
      if lowAmp:
        phase = 0.0
      # phaseOfs : initial value of down counter, by which phase shift is generated
      if phase >= 0.0:
        negPhaseMod = False
        phaseOfs = CLOCKS_77500HZ_DIV8 - int(round((CLOCKS_77500HZ_DIV8 * 2) * phase / 360.0, 0)) - 2
      else:
        negPhaseMod = True
        phaseOfs = int(round((CLOCKS_77500HZ_DIV8 * 2) * -phase / 360.0, 0)) - 2
      # FIFO data description
      # [31:24] PhaseOfs : set 148 for 0°, 135 for +15.6° and 11 for -15.6°
      # [23]    PhasePol : 0: >=0, 1: < 0
      # [22]    LowAmp   : 0: High Amplitude, 1: Low Amplitude
      # [21:0]  Clocks   : High Amplitude case - Clocks/4 incl. adj.  set 7750*149*2-1 for 7750 cycles
      #                    Low Amplitude case  - Clocks/16 incl. adj. set 7750*75-1    for 7750 cycles
      return ((phaseOfs & 0xff) << 24) | ((int(negPhaseMod) & 0b1) << 23) | ((int(lowAmp) & 0b1) << 22) | (clocks & 0x3fffff)
    # Generate 512 chips for phase modulation by LFSR
    def genLfsrChips(size) -> Iterator[int]:
      lfsr = 0
      for i in range(size):
        chip = lfsr & 0b1
        yield chip
        lfsr >>= 1
        if chip == 0b1 or lfsr == 0:
          lfsr ^= 0x110
    self.pinLed = pinLed
    self.pinModOutBase = pinModOutBase
    self.pioAsm = pioAsm
    # Create TimecodeSet
    self.TimecodeSet.create()
    # Preset data for FIFO
    self.LOW_7750  = genFifoData(cycles=7750,  lowAmp=True)   # Low  7750 cyc (100 ms) w/o phase mod
    self.LOW_15500 = genFifoData(cycles=15500, lowAmp=True)   # Low 15500 cyc (200 ms) w/o phase mod
    self.HIGH_7750 = genFifoData(cycles=7750,  lowAmp=False)  # High 7750 cyc (100 ms) w/o phase mod
    self.HIGH_560  = genFifoData(cycles=560,   lowAmp=False)  # High  560 cyc w/o phase mod
    # Preset series of phase modulation FIFO data by LFSR (120 cycle per chip * 512)
    HIGH_120_PM_CHIP = (
      genFifoData(cycles=120, lowAmp=False, phase=+15.6),  # High 120 cyc w/ +15.6 deg
      genFifoData(cycles=120, lowAmp=False, phase=-15.6),  # High 120 cyc w/ -15.6 deg
    )
    self.HIGH_61440_PM = (
      array.array('I', (HIGH_120_PM_CHIP[chip] for chip in genLfsrChips(512))),      # Non-inverted
      array.array('I', (HIGH_120_PM_CHIP[1 - chip] for chip in genLfsrChips(512))),  # Inverted
    )
    self.fifoErrorCheck = False
  def run(self, secToRun: int = 0) -> None:
    # === internal functions of run() (start) ===
    def sendTimecode(sm: rp2.StateMachine, vector: list[int], second: int = 0) -> None:
      def putSmFifo(arg: int | array.array) -> None:
        if self.fifoErrorCheck and sm.tx_fifo() == 0:
          # if ERROR, modulation output includes some blank terms where signal is not generated
          print("ERROR: PIO StateMachine TX_FIFO empty")
        sm.put(arg)
      def getPmValue(index: int, value: int) -> int:
        # overwrite value for phase modulation if needed
        if index < 10:
          value = 0b1
        elif index < 15 or index == 59:
          value = 0b0
        return value
      # Send one minute data
      for i in range(second, 60):
        value = vector[i]
        # no need to align second's edge here
        # because PIO should consume exact one minute of cycles
        # however please note that it runs with 'not so accurate' PLL frequency as a clock
        self.pinLed.toggle()
        if value == 0:
          putSmFifo(self.LOW_7750)
          putSmFifo(self.HIGH_7750)
        elif value == 1:
          putSmFifo(self.LOW_15500)
        else:  # synchronization
          putSmFifo(self.HIGH_7750)
          putSmFifo(self.HIGH_7750)
        putSmFifo(self.HIGH_61440_PM[getPmValue(i, value)])  # array.array
        putSmFifo(self.HIGH_560)
        # enable fifo error check from next round
        self.fifoErrorCheck = True
    # === internal functions of run() (end) ===
    lock = _thread.allocate_lock()
    ticksTimeout = utime.ticks_add(utime.ticks_ms(), secToRun * 1000)
    # start PIO
    sm = rp2.StateMachine(0, self.pioAsm, freq=SYSTEM_FREQ, sideset_base=self.pinModOutBase,)
    sm.active(False)
    sm.put(SYSTEM_FREQ // DCF77_CARRIER_FREQ // 8 - 2)  # write 148 integer to FIFO
    sm.exec('out(isr, 32)')                             # out to ISR (thus, store 148 to ISR)
    sm.active(True)
    # start modulation
    print(f'start DCF77 emission at {DCF77_CARRIER_FREQ} Hz')
    LocalTime.alignSecondEdge()
    self.TimecodeSet.getCurrent().genTimecode(utime.time() + 59)  # to generate time for next "minute"

    while True:
      def backgroundJob(lock: _thread.lock) -> None:  # Core1
        with lock:
          # print current Timecode
          current = self.TimecodeSet.getCurrent()
          print(f'Timecode: {current.t}  ', end='')
          vector = current.vector
          # Timecode format of https://www.dcf77logs.de/live
          print('-'.join(list(map(lambda v: ''.join(list(map(str, v))), [[0], vector[0:15], vector[15:21], vector[21:29], vector[29:36], vector[36:42], vector[42:45], vector[45:50], vector[50:59]]))))
          # generate next Timecode
          secs = current.secs
          next = self.TimecodeSet.getNext()
          next.genTimecode(current.secs + 60 - current.t.second)  # to generate time for next "minute" + adjust offset (should be only for the first time)
      # generate next Timecode in the backgournd (this should finish in short time, therefore omitting 'join' thread)
      _thread.start_new_thread(backgroundJob, (lock,))
      # send current Timecode (this is supposed to take just 60 seconds because putting FIFO is blocking)
      sendTimecode(sm, self.TimecodeSet.getCurrent().vector, self.TimecodeSet.getCurrent().t.second)
      if lock.locked():
        print("ERROR: backgroundJob has not finished yet")
      # swap buffers
      self.TimecodeSet.swap()
      # check secToRun
      if secToRun > 0 and utime.ticks_diff(utime.ticks_ms(), ticksTimeout) > 0:
        print(f'Finished {secToRun}+ sec.')
        break

def main() -> bool:
  print(f'System Frequency: {SYSTEM_FREQ} Hz')
  machine.freq(SYSTEM_FREQ)
  pinLed = machine.Pin("LED", machine.Pin.OUT)
  pinModOutP = machine.Pin(PIN_MOD_BASE, machine.Pin.OUT)
  pinModOutN = machine.Pin(PIN_MOD_BASE + 1, machine.Pin.OUT)
  pinLed.off()
  # connect WiFi
  if not connectWifi():
    return False
  # LED sign for WiFi connection
  for i in range(2 * 3):
    utime.sleep(0.1)
    pinLed.toggle()
  # NTP/RTC setting
  LocalTime.syncNtp()
  # disconnect WiFi
  disconnectWifi()
  # DCF77
  dcf77 = Dcf77(
    pinLed=pinLed,
    pinModOutBase=pinModOutP,
    pioAsm=pioAsmDcf77Carrier,
  )
  dcf77.run(SEC_TO_RUN)
  print('System reset to sync NTP again')
  utime.sleep(5)
  machine.reset()
  return True

if __name__ == '__main__':
  main()
