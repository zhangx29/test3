"""Called by the xlwings SampleCall macro in EquityXVA_Exposure.xlsm."""
import xlwings as xw

from eqxva.xl_entry import dispatch


def main():
    dispatch(xw.Book.caller())


if __name__ == "__main__":
    xw.Book(r"C:\Users\zhang\Documents\Xiaoli\AI\equity_pricing\excel\EquityXVA_Exposure.xlsm").set_mock_caller()
    main()
