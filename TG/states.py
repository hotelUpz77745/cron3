# ==============================================================================
# Path: TG/states.py
# Role: FSM состояния конечного автомата для диалогов Telegram бота
# ==============================================================================

from aiogram.fsm.state import State, StatesGroup


class TGStates(StatesGroup):
    waiting_for_add_symbol = State()
    waiting_for_add_strategy_choice = State()
    waiting_for_add_custom_invest = State()
    waiting_for_add_custom_lev = State()
    waiting_for_reassign_strat = State()
    waiting_for_strat_super_grid_json = State()
    waiting_for_del_symbol = State()
    waiting_for_edit_symbol = State()
    waiting_for_invest_size = State()
    waiting_for_avg_params = State()
    waiting_for_tp_params = State()
    waiting_for_base_json = State()
    waiting_for_super_grid_json = State()
    waiting_for_initial_balance = State()
    waiting_for_reset_confirm = State()
    waiting_for_scanner_json = State()
    waiting_for_close_all_confirm = State()
    waiting_for_notif_neg = State()
    waiting_for_notif_pos = State()
    waiting_for_autoclose_pos_th = State()
    waiting_for_autoclose_pos_inc = State()
    waiting_for_autoclose_neg_th = State()
    waiting_for_autoclose_neg_inc = State()
