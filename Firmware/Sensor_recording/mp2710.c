
#include "mp2710.h"


mp2710_t m_mp2710;

void mp2710_i2c_write(uint8_t reg, uint8_t data)
{
   
    MP2170_twi_data_write(MP2710_ADDR , reg, data);
}

void mp2710_i2c_read(uint8_t reg, uint8_t *p_data)
{
    MP2170_twi_data_read(MP2710_ADDR, reg, p_data);
}

void mp2710_write_reg(uint8_t reg)
{
    mp2710_t *p = &m_mp2710;
    uint8_t *p_reg = m_mp2710.regs.REG;
    uint8_t value;

    switch (reg) {
        case 0: {
            uint8_t vin_min = 0b0110;
            uint8_t iin_lim = 0b0111;
            if ((p->VIN_MIN <= 5080) && (p->VIN_MIN >= 3880)) {
                vin_min = (p->VIN_MIN - 3880) / 80;
            }
            if ((p->IIN_LIM <= 500) && (p->IIN_LIM >= 50)) {
                iin_lim = (p->IIN_LIM - 50) / 30;
            }
            value = (vin_min << 4) | (iin_lim);
            break;
        }
        case 1: {
            uint8_t vbatt_uvlo = 0b100;
            if ((p->VBATT_UVLO >= 2400) && (p->VBATT_UVLO <= 3030)) {
                vbatt_uvlo = (p->VBATT_UVLO - 2400) / 90;
            }
            value = (p->TRST_DGL << 6) |
                    (p->TRST_DUR << 5) |
                    (p->EN_HIZ << 4) |
                    (p->CEB << 3) |
                    (vbatt_uvlo);
            break;
        }
        case 2: {
            uint8_t icc = 0b00111111;
            if ((p->ICC <= 456) && (p->ICC >= 8)) {
                icc = (p->ICC - 2) / 2;
            }
            value = icc;
            break;
        }
        case 3: {
            uint8_t ipre = 0b0001;
            uint8_t iterm = 0b0001;
            if ((p->IPRE <= 31) && (p->IPRE >= 1)) {
                ipre = (p->IPRE - 1) / 2;
            }
            if ((p->ITERM <= 31) && (p->ITERM)) {
                iterm = (p->ITERM - 1) / 2;
            }
            value = (ipre << 4) | (iterm);
            break;
        }
        case 4: {
            uint8_t vbatt_reg = 0b101000;
            if ((p->VBATT_REG <= 4545) && (p->VBATT_REG >= 3600)) {
                vbatt_reg = (p->VBATT_REG - 3600) / 15;
            }
            value = (vbatt_reg << 2) | (p->VRECH & 0x01);
            break;
        }
        case 5:
            value = (p->EN_WD_DISCHG << 7) |
                    (p->WATCHDOG << 5) |
                    (p->EN_TERM << 4) |
                    (p->EN_TIMER << 3) |
                    (p->CHG_TMR << 1) |
                    (p->VBATT_PRE);
            break;
        case 6:
            value = (p->I2C_WATCHDOG_TIMER_RESET << 7) |
                    (p->TMR2X_EN << 6) |
                    (p->FET_DIS << 5) |
                    (p->PG_INT_CONTROL << 4) |
                    (p->EOC_INT_CONTROL << 3) |
                    (p->CHG_STATUS_INT_CONTROL << 2) |
                    (p->NTC_INT_CONTROL << 1) |
                    (p->BATTOVP_INT_CONTROL);
            break;
        case 7: {
            uint8_t vsys_reg = 0b1001;
            if ((p->VSYS_REG >= 4200) && (p->VSYS_REG <= 4950)) {
                vsys_reg = (p->VSYS_REG - 4200) / 50;
            }
            value = (p->LPM_EN << 7) |
                    (p->EN_VINLOOP << 6) |
                    (p->TJ_REG << 4) |
                    (vsys_reg);
            break;
        }
        // case 8: // Read only
            // break;
        // case 9: // Read only
            // break;
        case 10:
            value = (p->TESM_DGL << 4) |
                    (p->TEXSM_DGL << 2) |
                    (p->EN_NTC);
            break;
        case 11:
            value = (p->VHOT << 6) |
                    (p->VWARM << 4) |
                    (p->VCOOL << 2) |
                    (p->VCOOLER << 1) |
                    (p->VCOLD);
            break;
        case 12:
            value = (p->COOL_VSET << 7) |
                    (p->WARM_VSET << 6) |
                    (p->WARM_ISET << 4) |
                    (p->COOL_ISET << 2) |
                    (p->COOLER_ISET);
            break;
        case 13: {
            uint8_t idschg = 0b1001;
            if((p->IDSCHG >= 400) && (p->IDSCHG <= 3200)) {
                idschg = (p->IDSCHG - 200) / 200;
            }
            value = idschg;
            break;
        }
        // case 14: // OTP mode only
        //     break;
        default:
            break;
    }
    if ((reg!=8) && (reg!=9) && (reg!=14)) {
        if (value != p_reg[reg]) {
            p_reg[reg] = value;
            mp2710_i2c_write(reg, p_reg[reg]);
        }
    }
}

void mp2710_read_reg(uint8_t reg)
{
    mp2710_t *p = &m_mp2710;
    uint8_t *p_reg = m_mp2710.regs.REG;

    // // for test
    // uint8_t test_2710 = 0x12;
    // mp2710_i2c_write(reg, 0x17);
    // mp2710_i2c_read(reg, &test_2710);

    mp2710_i2c_read(reg, &p->regs.REG[reg]);
    // PRINT_DEBUG("REG%02X:0x%02X", reg, p->regs.REG[reg]);
    switch (reg) {
        case 0:
            p->VIN_MIN = ((p_reg[0] >> 4) * 80) + 3880;
            p->IIN_LIM = ((p_reg[0] & 0x0F) * 30) + 50;
            break;
        case 1:
            p->TRST_DGL   = (p_reg[1] >> 6) & 0x03;
            p->TRST_DUR   = (p_reg[1] >> 5) & 0x01;
            p->EN_HIZ     = (p_reg[1] >> 4) & 0x01;
            p->CEB        = (p_reg[1] >> 3) & 0x01;
            p->VBATT_UVLO = ((p_reg[1] & 0x07) * 90) + 2400;
            break;
        case 2:
            p->ICC = ((p_reg[2] & 0xFF) * 2) + 2;
            break;
        case 3:
            p->IPRE  = (((p_reg[3] >> 4) & 0x0F) * 2) + 1;
            p->ITERM = ((p_reg[3] & 0x0F) * 2) + 1;
            break;
        case 4:
            p->VBATT_REG = (((p_reg[4] >> 2) & 0x3F) * 15) + 3600;
            p->VRECH     = p_reg[4] & 0x01;
            break;
        case 5:
            p->EN_WD_DISCHG = (p_reg[5] >> 7) & 0x01;
            p->WATCHDOG     = (p_reg[5] >> 5) & 0x03;
            p->EN_TERM      = (p_reg[5] >> 4) & 0x01;
            p->EN_TIMER     = (p_reg[5] >> 3) & 0x01;
            p->CHG_TMR      = (p_reg[5] >> 1) & 0x03;
            p->VBATT_PRE    = p_reg[5] & 0x01;
            break;
        case 6:
            p->I2C_WATCHDOG_TIMER_RESET = (p_reg[6] >> 7) & 0x01;
            p->TMR2X_EN                 = (p_reg[6] >> 6) & 0x01;
            p->FET_DIS                  = (p_reg[6] >> 5) & 0x01;
            p->PG_INT_CONTROL           = (p_reg[6] >> 4) & 0x01;
            p->EOC_INT_CONTROL          = (p_reg[6] >> 3) & 0x01;
            p->CHG_STATUS_INT_CONTROL   = (p_reg[6] >> 2) & 0x01;
            p->NTC_INT_CONTROL          = (p_reg[6] >> 1) & 0x01;
            p->BATTOVP_INT_CONTROL      = p_reg[6] & 0x01;
            break;
        case 7:
            p->LPM_EN     = (p_reg[7] >> 7) & 0x01;
            p->EN_VINLOOP = (p_reg[7] >> 6) & 0x01;
            p->TJ_REG     = (p_reg[7] >> 4) & 0x03;
            p->VSYS_REG   = p_reg[7] & 0x0F;
            break;
        case 8:
            p->WATCHDOG_FAULT = (p_reg[8] >> 7) & 0x01;
            p->CHG_STAT       = (p_reg[8] >> 3) & 0x07;
            p->PPM_STAT       = (p_reg[8] >> 2) & 0x01;
            p->PG_STAT        = (p_reg[8] >> 1) & 0x01;
            p->THERM_STAT     = p_reg[8] & 0x01;
            break;
        case 9:
            p->REGISTER_RESET = (p_reg[9] >> 7) & 0x01;
            p->VIN_FAULT      = (p_reg[9] >> 6) & 0x01;
            p->THEM_SD        = (p_reg[9] >> 5) & 0x01;
            p->BAT_FAULT      = (p_reg[9] >> 4) & 0x01;
            p->STMR_FAULT     = (p_reg[9] >> 3) & 0x01;
            p->NTC_FAULT      = p_reg[9] & 0x07;
            break;
        case 10:
            p->TESM_DGL  = (p_reg[10] >> 4) & 0x03;
            p->TEXSM_DGL = (p_reg[10] >> 2) & 0x01;
            p->EN_NTC    = p_reg[10] & 0x01;
            break;
        case 11:
            p->VHOT    = (p_reg[11] >> 6) & 0x03;
            p->VWARM   = (p_reg[11] >> 4) & 0x03;
            p->VCOOL   = (p_reg[11] >> 2) & 0x03;
            p->VCOOLER = (p_reg[11] >> 1) & 0x01;
            p->VCOLD   = p_reg[11] & 0x01;
            break;
        case 12:
            p->COOL_VSET   = (p_reg[12] >> 7) & 0x01;
            p->WARM_VSET   = (p_reg[12] >> 6) & 0x01;
            p->WARM_ISET   = (p_reg[12] >> 4) & 0x03;
            p->COOL_ISET   = (p_reg[12] >> 2) & 0x03;
            p->COOLER_ISET = p_reg[12] & 0x03;
            break;
        case 13:
            p->IDSCHG = ((p_reg[13]) & 0x0F) * 200 + 200;
            break;
        case 14:
            p->SYSOVP_EN = (p_reg[14] >> 7) & 0x01;
            p->SHIP_METH = (p_reg[14] >> 5) & 0x01;
            p->ADDR      = (p_reg[14] >> 3) & 0x03;
            p->LP_FUNC   = (p_reg[14] >> 2) & 0x01;
            break;
        default:
            break;
    }
}

void mp2710_write_regs(void)
{
    for (uint8_t i=0; i<MP2710_REG_NUM; i++) {
        mp2710_write_reg(i);
    }
}

void mp2710_read_regs(void)
{
    for (uint8_t i=0; i<MP2710_REG_NUM; i++) {
        mp2710_read_reg(i);
    }
}

void mp2710_set_default_values(void)
{
    mp2710_t *p = &m_mp2710;
    // Vin parameter
    p->VIN_MIN = 5000; // Minimal Vin voltage: 5V
    // p->LPM_EN = 1; // Enable low-power mode; only connect battery, to reduce battery consumption

    // Vbattery parameter
    p->VBATT_UVLO = 3030; // Battery protection voltage
    p->VBATT_REG = 4095; // Battery regulation voltage
    p->TESM_DGL = 1; // Enter shipping mode deglitch time. 0:1s; 1:2s; 2:4s; 3:8s
    p->TEXSM_DGL = 0; // Exit shipping mode delay time. 0:80ms; 1:2s
    p->IDSCHG = 400; // Battery to system discharge current limit

    // Protection parameter
    p->EN_WD_DISCHG = 0; // Watchdog control in discharge mode disabled
    p->WATCHDOG = 0; // 0:disable; 1:40s; 2:80s; 3:160s

    // Vsys parameter
    p->VSYS_REG = 4200; // Ouput regulated voltage when Vin is present
    
    // charging parameter
    // p->CEB = 1; // Charge disabled
    p->ICC = 8; // 8mA fast char
    p->IPRE = 1;
    p->ITERM = 1;
    p->CHG_TMR = 0b00000010; // 8 hours fast charge timer

    // 初始化的时候 使能电池充电
    p->LPM_EN = 0; // disable low power mode
    p->CEB = 0; // enable charging
    p->FET_DIS = 0; 
    p->EN_HIZ = 0; 
}

void mp2710_enter_shipping_mode(void)
{
    mp2710_t *p = &m_mp2710;

    p->FET_DIS = 1;
    mp2710_write_reg(6);
}

bool mp2710_init(void)
{
    uint8_t *p_reg = m_mp2710.regs.REG;

    for (uint8_t i=0; i<200; i++) {
        mp2710_read_reg(0x0C);
        if (p_reg[0x0C] == 0x15) {
            // PRINT_INFO("MP2710 valid after %u loop", i);
            return true;
        }
    }

    // PRINT_ERROR("MP2710 invalid");
    return false;
}