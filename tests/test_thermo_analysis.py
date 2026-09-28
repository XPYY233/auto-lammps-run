import unittest
from auto_lammps.thermo_analysis import blocks, series


class ThermoTests(unittest.TestCase):
    def test_logged_total_and_units(self):
        content='Step Temp TotEng Press Volume\n0 300 -100 10000 8000\n10 301 -99 0 8001\nLoop time of 1\n'
        plan=dict(units='metal', dt_ps=.001, energy='logged_total',
                  stages=[dict(name='relaxation', block=0, first_step=0, last_step=10)])
        result=series(content, plan)
        self.assertEqual(result[0]['pressure_GPa'], 1)
        self.assertEqual(result[1]['time_ps'], .01)
        self.assertEqual(result[0]['total_energy_eV'], -100)
        self.assertEqual(result[0]['source_line'], 2)
        plan['stages'][0]['last_step']=20
        with self.assertRaisesRegex(ValueError, 'extent'):
            series(content, plan)

    def test_reconstructed_total_is_not_potential(self):
        content='Step Temp PotEng Pxx Pyy Pzz Lx Ly Lz\n0 300 -100 10000 20000 30000 10 20 30\nLoop time of 1\n'
        plan=dict(units='metal', dt_ps=.001, energy='reconstructed_total',
                  temperature_dof=3, boltzmann_eV_per_K=8.617343e-5,
                  stages=[dict(name='relaxation', block=0, first_step=0, last_step=0)])
        row=series(content, plan)[0]
        self.assertAlmostEqual(row['total_energy_eV'], -100+1.5*8.617343e-5*300)
        self.assertEqual(row['pressure_GPa'], 2)
        self.assertEqual(row['volume_A3'], 6000)

    def test_truncated_and_nonfinite_rejected(self):
        for content in ('Step Temp TotEng\n0 300 -100\n',
                        'Step Temp TotEng\n0 nan -100\nLoop time of 1\n',
                        'Step Temp TotEng\n0 300 -100\n0 300 -100\nLoop time of 1\n'):
            with self.assertRaises(ValueError):
                blocks(content)
