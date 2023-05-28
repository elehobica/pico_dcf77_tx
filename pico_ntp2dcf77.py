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
SYSTEM_FREQ = DCF77_CARRIER_FREQ * 800 * 2

# Pin Configuration
PIN_MOD = 2   # modulation (output for PIO) base, take 2 pins
PIN_CTRL = 3  # control (output for GPIO, input for PIO)

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
      return f'{self.year:04d}/{self.month:02d}/{self.mday:02d} {wday} {self.hour:02d}:{self.minute:02d}:{self.second:02d}'
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
@rp2.asm_pio(sideset_init = rp2.PIO.OUT_LOW)
def pioAsmDcf77Carrier():
  # generate 1/1600 frequency pulse against PIO clock with amplitude control by PWM
  #  jump pin from jmp_pin to control amplitude by PWM
  #  sideset pin from sideset_base to output modulation pulse
  # assume X = ISR = 198 at program entry to make 199-times loop

  wrap_target()
  # Start of Half1 (4 * 200 cycles)
  # High/Low switch by jmp_pin (0: High Amplitude, 1: Low Amplitude)
  # inst                   side    delay      # comment
  jmp(pin, 'Half1Low2')   .side(1)
  jmp('Half1High3')       .side(1)

  # High Amplitude (100%)
  label('Half1High1')
  nop()                   .side(1).delay(1)
  label('Half1High3')
  jmp(x_dec, 'Half1High1').side(1).delay(1)
  mov(x, isr)             .side(1).delay(2)   # load 198
  jmp('Half2')            .side(1)
  # End of H1 High Amplitude (100%)

  # Low Amplitude (25% by PWM)
  label('Half1Low1')
  nop()                   .side(1)
  label('Half1Low2')
  jmp(x_dec, 'Half1Low1') .side(0).delay(2)
  nop()                   .side(1)
  mov(x, isr)             .side(0).delay(2)   # load 198
  # End of H1 Low Amplitude (25%)

  # Start of Half2 (4 * 200 cycles)
  label('Half2')
  jmp(x_dec, 'Half2')     .side(0).delay(3)
  mov(x, isr)             .side(0).delay(3)   # load 198
  # End of Half2
  wrap()

# DCF77 class
class Dcf77:
  def __init__(self, lcTime: LocalTime, ctrlPins: tuple(machine.Pin), modOutPin: machine.Pin, pioAsm: Callable):
    self.lcTime = lcTime
    self.ctrlPins = ctrlPins  # for pulse control output (could be multiple, but PIO accepts only [0] as control input)
    self.modOutPin = modOutPin  # for modulation output
    self.pioAsm = pioAsm
    # dummy toggle because it spends much time (more than 0.6 sec) at first time
    self.__setLowerAmplitude(False)
    self.__setLowerAmplitude(True)
    self.__setLowerAmplitude(False)
  def __setLowerAmplitude(self, enable: bool) -> None:
    for ctrlPin in self.ctrlPins:
      ctrlPin.value(enable)  # (False for high amplitude, True for low amplitude)
  def __genTimecode(self, t: LocalTime.TimeTuple, **kwargs) -> list:
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
    # 80, 40, 20, 10: 10's digit
    # 8, 4, 2, 1    : 1's digit

    a1 = kwargs.get('a1', 0)
    z1 = kwargs.get('z1', 0)
    z2 = kwargs.get('z2', 1)
    a2 = kwargs.get('a2', 0)
    vector = []
    vector += self.__bin(0, name='M') + self.__bin(0, 14, name="CWB") + self.__bin(0, name='R')  # 0 ~ 15
    vector += self.__bin(a1) + self.__bin(z1) + self.__bin(z2) + self.__bin(a2) + self.__bin(1, name='S')  # 16 ~ 20
    vector += self.__bcd(t.minute, 7)  # 21 ~ 27
    vector += self.__bin(sum(vector[21:]), name='P1')  # 28
    vector += self.__bcd(t.hour, 6)  # 29 ~ 34
    vector += self.__bin(sum(vector[29:]), name='P2')  # 35
    vector += self.__bcd(t.mday, 6)  # 36 ~ 41
    vector += self.__bcd(t.weekday, 3)  # 42 ~ 44
    vector += self.__bcd(t.month, 5)  # 45 ~ 49
    vector += self.__bcd(t.year, 8)  # 50 ~ 57
    vector += self.__bin(sum(vector[36:]), name='P3')  # 58
    vector += self.__sync(name='MM')  # 59
    return vector
  def __sync(self, **kwargs: dict) -> list:
    return [2]
  def __bcd(self, value: int, numDigits: int = 4, **kwargs: dict) -> list:
    vector = []
    for bitPos in range(0, numDigits, 4):
      bcdValue = value % 10
      vector += [(bcdValue >> bitPos) & 0b1 for bitPos in range(4)]
      value //= 10
    return vector[:numDigits]
  def __bin(self, value: int, count: int = 1, **kwargs: dict) -> None:
    return [value & 0b1] * count
  def __sendTimecode(self, vector: list) -> None:
    for value in vector:
      self.lcTime.alignSecondEdge()
      if value == 0:  # bit 0
        self.__setLowerAmplitude(True)
        time.sleep(0.1)
      elif value == 1:  # bit 1
        self.__setLowerAmplitude(True)
        time.sleep(0.2)
      # otherwise, no amplitude modulation in case of synchronization (for 59s only) 
      self.__setLowerAmplitude(False)
  def run(self, secToRun: int = 0):
    ticksTimeout = time.ticks_add(time.ticks_ms(), secToRun * 1000)
    # start PIO
    sm = rp2.StateMachine(0, self.pioAsm, freq = SYSTEM_FREQ, jmp_pin = self.ctrlPins[0], sideset_base = self.modOutPin)
    sm.active(False)
    sm.put(198)              # write 198 integer to FIFO
    sm.exec('pull()')        # pull FIFO
    sm.exec('out(isr, 32)')  # out to ISR    (thus, store 198 to ISR)
    sm.exec('mov(x, isr)')   # move ISR to X (thus, store 198 to X)
    sm.active(True)
    # start modulation
    print(f'start DCF77 emission at {SYSTEM_FREQ / 1600} Hz')
    self.lcTime.alignSecondEdge()
    time.sleep(0.2)  # to make same condition as marker P0

    while True:
      t = self.lcTime.now(1)  # time for next second
      vector = self.__genTimecode(t)
      print(f'Timecode: {t}')
      self.__sendTimecode(vector[t.second:])  # apply offset (should be only for the first time)
      if secToRun > 0 and time.ticks_diff(time.ticks_ms(), ticksTimeout) > 0:
        print(f'Finished {secToRun}+ sec.')
        break

def main() -> bool:
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
    ctrlPins = (machine.Pin(PIN_CTRL, machine.Pin.OUT), led),
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
