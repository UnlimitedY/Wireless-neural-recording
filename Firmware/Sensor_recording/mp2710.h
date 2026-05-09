/******************************************************************************
 * @file   mp2710.h
 * @brief  MP2710是一款高集成度、高效率的单节锂电池充电和系统电源管理芯片
 *         支持输入电压保护、电池充放电管理、温度保护等功能
 * @author Zhu Shaoling
 * @email  colincr@163.com
 *****************************************************************************/
#ifndef MP2710_H__
#define MP2710_H__
/*---------------------------------------------------------------------------*/
#include "sensor_driver.h"
/*---------------------------------------------------------------------------*/
#define MP2710_ADDR    (0x08)
#define MP2710_REG_NUM (15)
/*---------------------------------------------------------------------------*/
typedef struct {
    union {
        struct {
            uint8_t REG00; // R/W Input source control register
            uint8_t REG01; // R/W Start-up configuration register
            uint8_t REG02; // R/W Charge current control register
            uint8_t REG03; // R/W Termination current
            uint8_t REG04; // R/W Charge voltage control register
            uint8_t REG05; // R/W Charge termination/timer control register
            uint8_t REG06; // R/W BATFET control and INT mask
            uint8_t REG07; // R/W Low-power mode and system voltage register
            uint8_t REG08; // R   Statu register
            uint8_t REG09; // R   Fault register
            uint8_t REG0A; // R/W Shipping mode delay and NTC control
            uint8_t REG0B; // R/W NTC temperature threshold
            uint8_t REG0C; // R/W NTC action
            uint8_t REG0D; // R/W Battery discharge current limit
            uint8_t REG0E; // R/W Write only for OTP use
        } reg_map;
        uint8_t REG[15];
    } regs;
    // REG00 限制输入电压电流范围
    uint16_t VIN_MIN; // 3.88V 0000 
    uint16_t IIN_LIM; // 500mA default 1111
    // REG01
    uint8_t  TRST_DGL; // 16s default 
    uint8_t  TRST_DUR; // 2s default
    uint8_t  EN_HIZ; // 0 disable：open vin
    uint8_t  CEB; // 0 charging enabled： open battery charging 
    uint16_t VBATT_UVLO; // 3.03V 111
    // REG02
    uint16_t ICC; // 30mA 0000 1110 ： 30mAH battery charging current
    // REG03
    uint8_t  IPRE; // 3mA default
    uint8_t  ITERM; // 3mA default
    // REG04
    uint16_t VBATT_REG; // 4.2V default
    uint8_t  VRECH; // 200mV default； below 4.2V, recharging
    // REG05
    uint8_t  EN_WD_DISCHG; // 0 disable
    uint8_t  WATCHDOG; // 00 disable
    uint8_t  EN_TERM; // 1 default
    uint8_t  EN_TIMER; // 1 default
    uint8_t  CHG_TMR; // 8 hours ：10 fast charging cycle
    uint8_t  VBATT_PRE; // 1 ： 3V default pre-charge -》 fast-charge
    // REG06 all default ； *2 safer timer during ppm
    uint8_t  I2C_WATCHDOG_TIMER_RESET;
    uint8_t  TMR2X_EN;
    uint8_t  FET_DIS; // control by software to enter shipping mode； set to 1; 0 default
    uint8_t  PG_INT_CONTROL;
    uint8_t  EOC_INT_CONTROL;
    uint8_t  CHG_STATUS_INT_CONTROL;
    uint8_t  NTC_INT_CONTROL;
    uint8_t  BATTOVP_INT_CONTROL;
    // REG07
    uint8_t  LPM_EN; // default disable low power mode
    uint8_t  EN_VINLOOP; // default
    uint8_t  TJ_REG; // default
    uint16_t VSYS_REG; // 4.2V 0000
    // REG08 read-only register； operation status
    uint8_t  WATCHDOG_FAULT;
    uint8_t  CHG_STAT;
    uint8_t  PPM_STAT;
    uint8_t  PG_STAT;
    uint8_t  THERM_STAT;
    // REG09 read-only register； fault status
    uint8_t  REGISTER_RESET; // reset all registers
    uint8_t  VIN_FAULT;
    uint8_t  THEM_SD;
    uint8_t  BAT_FAULT;
    uint8_t  STMR_FAULT;
    uint8_t  NTC_FAULT;
    // REG0A 
    uint8_t  TESM_DGL; // 1s enter shipping mode default
    uint8_t  TEXSM_DGL; // 80ms default exit shipping mode default
    uint8_t  EN_NTC; // disable default
    // REG0B NTC default
    uint8_t  VHOT;
    uint8_t  VWARM;
    uint8_t  VCOOL;
    uint8_t  VCOOLER;
    uint8_t  VCOLD;
    // REG0C NTC action default
    uint8_t  COOL_VSET;
    uint8_t  WARM_VSET;
    uint8_t  WARM_ISET;
    uint8_t  COOL_ISET;
    uint8_t  COOLER_ISET;
    // REG0D battery discharge sys limit
    uint16_t IDSCHG; // default 2000mA
    // REG0E
    uint8_t  SYSOVP_EN; // default 
    uint8_t  SHIP_METH; // 0 default shipping mode by software
    uint8_t  ADDR; // 08h
    uint8_t  LP_FUNC; // available default low power function
} mp2710_t;

extern mp2710_t m_mp2710;
/*---------------------------------------------------------------------------*/
// function
void mp2710_i2c_write(uint8_t reg, uint8_t data);
void mp2710_i2c_read(uint8_t reg, uint8_t *p_data);
void mp2710_write_reg(uint8_t reg);
void mp2710_read_reg(uint8_t reg);
void mp2710_write_regs(void);
void mp2710_read_regs(void);
void mp2710_set_default_values(void);
void mp2710_enter_shipping_mode(void);
bool mp2710_init(void);


#endif // MP2710_H__
