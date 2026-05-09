
/* 05/01/2021 Copyright Tlera Corporation
 *  
 *  Created by Kris Winer
 *  
 *  The LC709204F is a low-cost, low-power 1S LiPo battery fuel gauge.
 *  
 *  Library may be used freely and without limit with attribution.
 *  
 */
 
#include "LC709204F.h"

nrfx_err_t getChipID(uint16_t *chip_id) // return 30
{
    return LC_twi_data_read(LC709204F_ADDRESS, LC709204F_APT, chip_id);
}

nrfx_err_t LC_init(void)
{
    uint16_t data[8] = {0x1313 , // battery type-01: Nominal voltage:3.7V' charging voltage: 4.2V
                       0x0000 ,
                       0x0000 ,
                       0x00A0 ,
                       0x0E10 ,
                       0x0C3C ,
                       0x0001 ,
                       0x0000};
    nrfx_err_t err;
    err = LC_twi_data_write(LC709204F_ADDRESS, LC709204F_APA, data[0]);                 // set APA for 50 mAH capacity
    if (err != NRFX_SUCCESS) {
        return err;
    }
    err = LC_twi_data_write(LC709204F_ADDRESS, LC709204F_CHANGE_PARAM,  data[1]);       // set change of parameter for 3.7 V 1S LiPo : Type -01
    if (err != NRFX_SUCCESS) {
        return err;
    }
    err = LC_twi_data_write(LC709204F_ADDRESS, LC709204F_STATUS_BIT,    data[2]);       // 
    if (err != NRFX_SUCCESS) {
        return err;
    }

    // // Set up interrupts
        // LC_twi_data_write(LC709204F_ADDRESS, LC709204F_ALARM_LOW_RSOC,  data[3]);     // set to alarm when RSOC falls below 10%
        // //0x0E10 = 3600
        // LC_twi_data_write(LC709204F_ADDRESS, LC709204F_ALARM_LOW_CELL_VLT,  data[4]); // set to alarm when battery voltage falls below 3.6 V
        // //0x0C3C = 3132 = 2732 + 400 so alarms at 40 C
        // LC_twi_data_write(LC709204F_ADDRESS, LC709204F_ALARM_HIGH_TEMP,  data[5]);    // set to alarm when temperature rises above 40 C

    // Set power mode and clear battery status
    err = LC_twi_data_write(LC709204F_ADDRESS, LC709204F_IC_POWERMODE,  data[6]);       // set to operate mode
    if (err != NRFX_SUCCESS) {
        return err;
    }
    return LC_twi_data_write(LC709204F_ADDRESS, LC709204F_BATTERY_STATUS,  data[7]);     // reset battery status for some flags indicating spectial events
}


nrfx_err_t LC_sleep(void)
{
    uint16_t data = 0x0002;
    return LC_twi_data_write(LC709204F_ADDRESS, LC709204F_IC_POWERMODE, data);      // set to sleep mode
}


nrfx_err_t LC_operate(void)
{
    uint16_t data = 0x0001;
    return LC_twi_data_write(LC709204F_ADDRESS, LC709204F_IC_POWERMODE,  data);       // set to operate mode
}


nrfx_err_t LC_getCellVoltage(uint16_t *databuf)
{
    return LC_twi_data_read(LC709204F_ADDRESS, LC709204F_CELL_VOLTAGE ,databuf);
}

nrfx_err_t LC_getRSOC(uint16_t *databuf)
{
    return LC_twi_data_read(LC709204F_ADDRESS, LC709204F_RSOC ,databuf);
}


nrfx_err_t LC_getITE(uint16_t *databuf)
{
    return LC_twi_data_read(LC709204F_ADDRESS, LC709204F_ITE ,databuf);
}


nrfx_err_t LC_getStatus(uint16_t *databuf)
{
    return LC_twi_data_read(LC709204F_ADDRESS, LC709204F_BATTERY_STATUS ,databuf);
}





nrfx_err_t LC_clearStatus(void)
{
    uint16_t data = 0x0000;
    return LC_twi_data_write(LC709204F_ADDRESS, LC709204F_BATTERY_STATUS,  data);     // reset battery status
}


nrfx_err_t LC_timetoEmpty(uint16_t *databuf)
{
    return LC_twi_data_read(LC709204F_ADDRESS, LC709204F_TIME_TO_EMPTY ,databuf);
}


nrfx_err_t LC_stateofHealth(uint16_t *databuf)
{
    return LC_twi_data_read(LC709204F_ADDRESS, LC709204F_STATE_OF_HEALTH ,databuf);
}


nrfx_err_t LC_setTemperature(uint16_t temperature)
{
    return LC_twi_data_write(LC709204F_ADDRESS, LC709204F_CELL_TEMP, temperature); // input cell (MCU) temperature in 0.1 K
}


nrfx_err_t LC_getTemperature(uint16_t *databuf)
{
    return LC_twi_data_read(LC709204F_ADDRESS, LC709204F_CELL_TEMP ,databuf);
}
