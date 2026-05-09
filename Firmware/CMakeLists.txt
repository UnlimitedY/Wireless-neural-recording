cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

project(test1)

target_sources(app PRIVATE src/main.c RHD_Recording/RHDRecording.c RHD_Recording/OnlineFilter.c ESB_wireless/ESB_wireless.c Sensor_recording/LC709204F.c Sensor_recording/LSM6DS3.c Sensor_recording/sensor_driver.c Sensor_recording/mp2710.c)
