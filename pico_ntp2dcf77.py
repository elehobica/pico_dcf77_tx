# NTP to DCF77
#  for Raspberry Pi Pico W
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

import machine
import rp2
import time 
import utime
import network
import ntptime

# write ssid and password as 'secrets' dict in secrets.py
from secrets import secrets

# CET offset
TZ_CET_OFS = 1

# DCF77 carrier frequency 77500  Hz
DCF77_CARRIER_FREQ = 77500

# System Frequency to set multiplier of DCF77 frequency
SYSTEM_FREQ = DCF77_CARRIER_FREQ * 600 * 2

# Pin Configuration
PIN_MOD = 2   # modulation (output for PIO) base, take 2 pins

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
    time.sleep(1)
  else:
    print('WiFi not connected')
    return False
  return True

def disconnectWifi():
  wlan = network.WLAN(network.STA_IF)
  wlan.deinit()

# LocalTime class for NTP and RTC
class LocalTime:
  CET = 1
  # utility to handle time tuple
  class TimeTuple:
    def __init__(self, timeTuple: tuple):
      self.year, self.month, self.mday, self.hour, self.minute, self.second, self.weekday, self.yearday = timeTuple
    def __str__(self):
      wday = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[self.weekday]
      month = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')[self.month-1]
      return f'{wday} {month} {self.mday:02d}, {self.year:03d} {self.hour:02d}:{self.minute:02d}:{self.second:02d}'
  def __init__(self, timeZone: int):
    self.ntpTime = self.__setNtpTime(timeZone)
    print(f'NTP: {self.ntpTime}')
    self.rtcTime = self.__setRtc(self.ntpTime)
    print(f'RTC: {self.rtcTime}')
  def __setNtpTime(self, timeZone: int) -> TimeTuple:
    time.sleep(1)
    try:
      ntptime.settime()
    except OSError as e:
      if e.args[0] == 110:
        # reset when OSError: [Errno 110] ETIMEDOUT
        print(e)
        time.sleep(5)
        machine.reset()
    now = time.time()
    if timeZone == LocalTime.CET:
      # switch CET or CEST (https://github.com/lammersch/ntp-timer/)
      t = time.localtime()
      tt = self.TimeTuple(t)
      HHMarch   = time.mktime((tt.year,3 ,(31 - (int(5 * tt.year/4 + 4)) % 7), 1, 0, 0, 0, 0, 0))  # Time of March change to CEST
      HHOctober = time.mktime((tt.year,10,(31 - (int(5 * tt.year/4 + 1)) % 7), 1, 0, 0, 0, 0, 0))  # Time of October change to CET
      if now < HHMarch:
        tzNow = time.localtime(now + timeZone * 3600)
      elif now < HHOctober:
        tzNow = time.localtime(now + (timeZone + 1) * 3600)
      else:
        tzNow = time.localtime(now + timeZone * 3600)
    else:
      tzNow = time.localtime(now + timeZone * 3600)
    return self.TimeTuple(utime.localtime(utime.mktime(tzNow)))
  def __setRtc(self, t: TimeTuple) -> TimeTuple:
    machine.RTC().datetime((t.year, t.month, t.mday, t.weekday+1, t.hour, t.minute, t.second, 0))
    time.sleep(1)  # wait to be reflected
    return self.TimeTuple(time.localtime())
  def now(self, offset: int = 0) -> TimeTuple:
    return self.TimeTuple(time.localtime(time.time() + offset))
  def alignSecondEdge(self):
    t = self.now()
    while t.second == self.now().second:
      time.sleep_ms(1)

# PIO program
@rp2.asm_pio(
  sideset_init = rp2.PIO.OUT_LOW,
  out_shiftdir = rp2.PIO.SHIFT_RIGHT,
  autopull = True,
  pull_thresh = 32,
  fifo_join = rp2.PIO.JOIN_TX,
)
def pioAsmDcf77Carrier():
  # generate 1/1200 frequency pulse against PIO clock with amplitude control by PWM
  #  jump pin from jmp_pin to control amplitude by PWM
  #  sideset pin from sideset_base to output modulation pulse
  # assume ISR = 148 at program entry to make 149-times loop (corresponding to 1cyc = 1200clk / 4clk / 2turn = 150 loop)
  # use X for phase modulation by designating initial value of loop counter which determines the cycle
  # High amplitude: 100% by PWM 0/4 - 4/4
  # Low amplitude :  25% by PWM 3/8 - 5/8

  # inst                       side    delay     # comment
  nop()                       .side(0)           # initialize pin as 0

  # ---------------------- no side-set option to keep previous pin status (start)
  label('ClocksEnd')
  out(y, 22)                                     # get next Clocks4/8
  out(x, 1)                                      # get LowAmp (0: High Amp, 1: Low Amp)
  jmp(x_dec, 'LowAmpH1Setup')
  out(x, 1)                                      # get PhasePol (0: >= 0, 1: < 0)
  jmp(x_dec, 'HighAmpH2Setup')
  # HighAmpH1Setup
  out(x, 8)                                      # get PhaseOfs (should be 0 deg or +15.6 deg)
  jmp(y_dec, 'HighAmpH1_2')   .side(1)           # always jmp

  label('HighAmpH2Setup')
  out(x, 8)                                      # get PhaseOfs (should be -15.6 deg)
  jmp(y_dec, 'HighAmpH2_2')   .side(0)           # always jmp

  label('LowAmpH1Setup')
  out(x, 1)                                      # get PhasePol (don't use)
  out(x, 8)                                      # get PhaseOfs (should be 0 deg)
  jmp('LowAmpH1')
  # ---------------------- no side-set option to keep previous pin status (end)

  wrap_target()
  # Start of H1 of High Amplitude (4 * 150 clocks if no phase modulation) pulse shape: ~~~~
  label('HighAmpH1')
  jmp(y_dec, 'HighAmpH1_2')   .side(1)
  jmp('ClocksEnd')            .side(1)
  label('HighAmpH1_2')
  jmp(x_dec, 'HighAmpH1')     .side(1).delay(2)
  mov(x, isr)                 .side(1).delay(3)  # load 148
  # End of H1 of High Amplitude
  # Start of H2 of High Amplitude (4 * 150 clocks if no phase modulation) pulse shape: ____
  label('HighAmpH2')
  jmp(y_dec, 'HighAmpH2_2')   .side(0)
  jmp('ClocksEnd')            .side(0)
  label('HighAmpH2_2')
  jmp(x_dec, 'HighAmpH2')     .side(0).delay(2)
  mov(x, isr)                 .side(0).delay(3)  # load 148
  wrap()
  # End of H2 of High Amplitude

  label('LowAmpH1_6')
  jmp(x_dec, 'LowAmpH1')      .side(0).delay(2)  # always jmp
  # Start of H1 of Low Amplitude (8 * 75 clocks only for no phase modulation) pulse shape: ~~~~~___
  label('LowAmpH1')
  jmp(x_dec, 'LowAmpH1_6')    .side(1).delay(4)
  mov(x, isr)                 .side(0).delay(1)  # load 148
  jmp('LowAmpH2')             .side(0)
  # End of H1 of Low Amplitude

  label('LowAmpH2_5')
  jmp(x_dec, 'LowAmpH2')      .side(0).delay(3)  # always jmp
  # Start of H2 of Low Amplitude (8 * 75 clocks only for no phase modulation) pulse shape: _~~~____
  label('LowAmpH2')
  jmp(y_dec, 'LowAmpH2_2')    .side(0)
  jmp('ClocksEnd')            .side(0)           # lacking pulse only last time
  label('LowAmpH2_2')
  jmp(x_dec, 'LowAmpH2_5')    .side(1).delay(2)
  mov(x, isr)                 .side(0).delay(2)  # load 148
  jmp('LowAmpH1')             .side(0)
  # End of H2 of Low Amplitude

# DCF77 class
class Dcf77:
  def __init__(self, lcTime: LocalTime, modOutPin: machine.Pin, pioAsm: Callable):
    self.lcTime = lcTime
    self.modOutPin = modOutPin  # for modulation output
    self.pioAsm = pioAsm
  def __genTimecode(self, t: LocalTime.TimeTuple, **kwargs) -> list:
    ## Timecode generating functions ##
    def sync(**kwargs: dict) -> list:
      return [2]
    def bcd(value: int, numDigits: int = 4, **kwargs: dict) -> list:
      vector = []
      for bitPos in range(0, numDigits, 4):
        bcdValue = value % 10
        vector += [(bcdValue >> bitPos) & 0b1 for bitPos in range(4)]
        value //= 10
      return vector[:numDigits]
    def bin(value: int, count: int = 1, **kwargs: dict) -> list:
      return [value & 0b1] * count

    ## Timecode ##
    # 00: M: Start of minite (0)
    # 01 ~ 14: Civil warning bits: (send 0s in this program)
    # 15: R: Call bit: abnormal transmitter operation (send 0 in this program)
    # 16: A1: Summer time annousement. Set during hour before change.
    # 17: Z1: CEST in effect
    # 18: Z2: CET in effect
    # 19: A2: Leap second announcement. Set during hor before leap second.
    # 20: S: Start of encoded time (1)
    # 21 ~ 27: Minite (00 ~ 59) BCD 1, 2, 4, 8, 10, 20, 40
    # 28: P1: Even parity of 21 ~ 28
    # 29 ~ 34: Hour (00 ~ 23) BCD 1, 2, 4, 8, 10, 20
    # 35: P2: Even parity of 29 ~ 35
    # 36 ~ 41: Day of month (01 ~ 31) BCD 1, 2, 4, 8, 10, 20
    # 42 ~ 44: Wday (1: Mon, 7: Sun) BCD 1, 2, 4
    # 45 ~ 49: Month (01 ~ 12) BCD 1, 2, 4, 8, 10
    # 50 ~ 57: Year(2) (00 ~ 99) BCD 1, 2, 4, 8, 10, 20, 40, 80
    # 58: P3: Even parity of 36 ~ 58
    # 59: Minite mark (No amplitude modulation)

    # BCD
    # 10, 20, 40, 80: 10's digit
    # 1, 2, 4, 8    : 1's digit

    a1 = kwargs.get('a1', 0)
    z1 = kwargs.get('z1', 0)
    z2 = kwargs.get('z2', 1)
    a2 = kwargs.get('a2', 0)
    vector = []
    vector += bin(0, name='M') + bin(0, 14, name="CWB") + bin(0, name='R')  # 0 ~ 15
    vector += bin(a1) + bin(z1) + bin(z2) + bin(a2) + bin(1, name='S')  # 16 ~ 20
    vector += bcd(t.minute, 7)  # 21 ~ 27
    vector += bin(sum(vector[21:]), name='P1')  # 28
    vector += bcd(t.hour, 6)  # 29 ~ 34
    vector += bin(sum(vector[29:]), name='P2')  # 35
    vector += bcd(t.mday, 6)  # 36 ~ 41
    vector += bcd(t.weekday, 3)  # 42 ~ 44
    vector += bcd(t.month, 5)  # 45 ~ 49
    vector += bcd(t.year, 8)  # 50 ~ 57
    vector += bin(sum(vector[36:]), name='P3')  # 58
    vector += sync(name='MM')  # 59
    return vector
  def run(self, secToRun: int = 0) -> None:
    def sendTimecode(sm: rp2.StateMachine, vector: list, second: int = 0) -> None:
      def genFifoData(clocks: int, lowAmp: bool, negPhaseMod: bool, phaseOfs: int) -> int:
        # FIFO data description
        # [31:24] PhaseOfs : set 148 for 0°, 135 for +15.6° and 11 for -15.6°
        # [23]    PhasePol : 0: >=0, 1: < 0
        # [22]    LowAmp   : 0: High Amplitude, 1: Low Amplitude
        # [21:0]  Clocks   : High Amplitude case - Clocks/4 incl. adj.  set 7750*149*2-1 for 7750 cycles
        #                    Low Amplitude case  - Clocks/16 incl. adj. set 7750*75-1    for 7750 cycles
        return ((phaseOfs & 0xff) << 24) | ((int(negPhaseMod) & 0b1) << 23) | ((int(lowAmp) & 0b1) << 22) | (clocks & 0x3fffff)
      # Preset data for FIFO
      LOW_7750            = genFifoData(7750*75-1,    True,  False, 149-1)  # Low 7750 cyc (100 ms) w/o phase mod
      LOW_15500           = genFifoData(15500*75-1,   True,  False, 149-1)  # Low 15500 cyc (200 ms) w/o phase mod
      HIGH_7750           = genFifoData(7750*149*2-1, False, False, 149-1)  # High 7750 cyc (100 ms) w/o phase mod
      HIGH_120_PLUS_15P6  = genFifoData(120*149*2-1,  False, False, 136-1)  # High 120 cyc w/ +15.6 deg
      HIGH_120_MINUS_15P6 = genFifoData(120*149*2-1,  False, True,  12-1)   # High 120 cyc w/ -15.6 deg
      HIGH_560            = genFifoData(560*149*2-1,  False, False, 149-1)  # High 560 cyc w/o phase mod
      # Send one minite data
      for value in vector[second:]:
        self.lcTime.alignSecondEdge()
        if value == 0:
          sm.put(LOW_7750)
          sm.put(HIGH_7750)
          for i in range(256):  # Phase modulation 120cyc * 512
            # dummy (not implemented yet)
            sm.put(HIGH_120_PLUS_15P6)
            sm.put(HIGH_120_MINUS_15P6)
          sm.put(HIGH_560)
        elif value == 1:
          sm.put(LOW_15500)
          for i in range(256):  # Phase modulation 120cyc * 512
            # dummy (not implemented yet)
            sm.put(HIGH_120_PLUS_15P6)
            sm.put(HIGH_120_MINUS_15P6)
          sm.put(HIGH_560)
        else:  # synchronization
          for i in range(10):
            sm.put(HIGH_7750)
    # run
    ticksTimeout = time.ticks_add(time.ticks_ms(), secToRun * 1000)
    # start PIO
    sm = rp2.StateMachine(0, self.pioAsm, freq = SYSTEM_FREQ, sideset_base = self.modOutPin,)
    sm.active(False)
    sm.put(SYSTEM_FREQ // DCF77_CARRIER_FREQ // 8 - 2)  # write 148 integer to FIFO
    sm.exec('out(isr, 32)')  # out to ISR    (thus, store 148 to ISR)
    sm.active(True)
    # start modulation
    print(f'start DCF77 emission at {DCF77_CARRIER_FREQ} Hz')
    self.lcTime.alignSecondEdge()

    while True:
      t = self.lcTime.now(1)  # time for next second
      vector = self.__genTimecode(t)
      print(f'Timecode: {t}')
      sendTimecode(sm, vector, t.second)  # apply offset (should be only for the first time)
      if secToRun > 0 and time.ticks_diff(time.ticks_ms(), ticksTimeout) > 0:
        print(f'Finished {secToRun}+ sec.')
        break

def main() -> bool:
  print(f'System Frequency: {SYSTEM_FREQ} Hz')
  machine.freq(SYSTEM_FREQ)  # recommend multiplier of 77500*2 to avoid jitter
  led = machine.Pin("LED", machine.Pin.OUT)
  led.off()
  # connect WiFi
  if not connectWifi():
    return False
  # LED sign for WiFi connection
  for i in range(2 * 3):
    time.sleep(0.1)
    led.toggle()
  # NTP/RTC setting
  lcTime = LocalTime(LocalTime.CET)
  # disconnect WiFi
  disconnectWifi()
  # DCF77
  dcf77 = Dcf77(
    lcTime = lcTime,
    modOutPin = machine.Pin(PIN_MOD, machine.Pin.OUT),
    pioAsm = pioAsmDcf77Carrier,
  )
  dcf77.run(SEC_TO_RUN)
  print('System reset to sync NTP again')
  time.sleep(5)
  machine.reset()
  return True

if __name__ == '__main__':
  main()
